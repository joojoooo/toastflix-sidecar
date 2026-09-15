import asyncio
import json
import hmac
import math
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from activity import ActivityLog, TrafficLogMiddleware
from audio import AudioStore
from offsets import OffsetStore
from playback import PlaybackRegistry
from security import SessionManager, request_token, resolves_publicly
from sync import SyncEngine


APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("SIDECAR_CACHE_DIR", APP_DIR / "data"))
PUBLIC_BASE_URL = os.getenv("SIDECAR_PUBLIC_URL", "").strip().rstrip("/")
SESSION_TTL = int(os.getenv("SIDECAR_SESSION_TTL", "21600"))
FIXED_TOKEN = os.getenv("SIDECAR_FIXED_TOKEN", "").strip()
AUDIO_PROXY = os.getenv("SIDECAR_AUDIO_PROXY", "").strip()
OFFSET_API_URL = os.getenv("OFFSET_API_URL", "").strip()
OFFSET_API_TOKEN = os.getenv("OFFSET_API_TOKEN", "").strip()
BOOTSTRAP_KEY = os.getenv("SIDECAR_BOOTSTRAP_KEY", "").strip()
ADMIN_TOKEN = os.getenv("SIDECAR_ADMIN_TOKEN", "").strip()
ACTIVITY_MAX_ROWS = int(os.getenv("SIDECAR_ACTIVITY_MAX_ROWS", "5000"))

activity = ActivityLog(str(CACHE_DIR / "activity.db"), ACTIVITY_MAX_ROWS)
audio = AudioStore(str(CACHE_DIR / "audio"), proxy=AUDIO_PROXY, activity=activity)
offsets = OffsetStore(
    str(CACHE_DIR / "offsets.db"), OFFSET_API_URL, OFFSET_API_TOKEN, activity=activity
)
sessions = SessionManager(SESSION_TTL, FIXED_TOKEN)
sync_engine = SyncEngine(audio, offsets, AUDIO_PROXY)
playbacks = PlaybackRegistry()

app = FastAPI(title="Toast Audio Sidecar", version="1.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Sidecar-Offset", "X-Sidecar-Offset-Source", "X-Sidecar-Revision"],
)
app.add_middleware(TrafficLogMiddleware, activity=activity)


def _require_session(request: Request, body: dict | None = None) -> str:
    token = request_token(request, body)
    if not sessions.valid(token):
        raise HTTPException(status_code=401, detail="sidecar session required")
    return token


def _require_admin(request: Request):
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="dashboard is locked; configure SIDECAR_ADMIN_TOKEN",
        )
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid dashboard token")


def _offset_values(body: dict) -> tuple[float, float]:
    try:
        offset = float(body.get("offset"))
        rate = float(body.get("rate", 1.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="offset and rate must be numbers") from exc
    if not math.isfinite(offset) or abs(offset) > 600:
        raise HTTPException(status_code=400, detail="offset must be between -600 and 600 seconds")
    if not math.isfinite(rate) or not 0.998 <= rate <= 1.002:
        raise HTTPException(status_code=400, detail="rate must be between 0.998 and 1.002")
    return offset, rate


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _audio_url(request: Request, hid: str, token: str, offset: float = 0.0, rate: float = 1.0) -> str:
    query = urlencode({"o": int(round(offset * 1000)), "r": int(round(rate * 1_000_000_000)), "t": token})
    return f"{_base_url(request)}/dual/aud/{hid}/audio.m3u8?{query}"


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "toast-audio-sidecar",
        "public_url": PUBLIC_BASE_URL or None,
        "dashboard": "/dashboard",
        "dashboard_configured": bool(ADMIN_TOKEN),
    }


def _dashboard_file(name: str):
    response = FileResponse(APP_DIR / "static" / name)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return _dashboard_file("dashboard.html")


@app.get("/dashboard.css", include_in_schema=False)
async def dashboard_css():
    return _dashboard_file("dashboard.css")


@app.get("/dashboard.js", include_in_schema=False)
async def dashboard_js():
    return _dashboard_file("dashboard.js")


@app.post("/session")
async def create_session(request: Request):
    if BOOTSTRAP_KEY:
        supplied = request.headers.get("x-sidecar-bootstrap", "")
        if supplied != BOOTSTRAP_KEY:
            raise HTTPException(status_code=401, detail="bootstrap key required")
    sessions.cleanup()
    token, expires_at = sessions.issue()
    return {"token": token, "expires_at": expires_at, "ttl_seconds": sessions.ttl_seconds}


@app.post("/dual/aprep")
async def prepare_audio(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    try:
        hid = await audio.register(
            playlist=str(body.get("playlist") or ""),
            key_b64=str(body.get("key") or ""),
            media_key=str(body.get("mediaKey") or ""),
            language=str(body.get("lang") or ""),
            base_url=str(body.get("baseUrl") or ""),
            headers=body.get("headers") if isinstance(body.get("headers"), dict) else {},
        )
        metadata = audio.metadata(hid)
        for url in (metadata["segs"][0], metadata["segs"][-1]):
            if not await resolves_publicly(url):
                raise ValueError("audio source does not resolve publicly")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    language = str(body.get("lang") or "").lower()
    metadata = audio.metadata(hid)
    playbacks.register(hid, token, metadata, cached_audio=False, request_metadata=body)
    return JSONResponse({
        "hid": hid,
        "url": _audio_url(request, hid, token),
        "language": language,
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
    })


@app.post("/dual/acache")
async def cached_audio(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    hid = audio.find_cached(str(body.get("mediaKey") or ""), str(body.get("lang") or "").lower())
    if not hid:
        raise HTTPException(status_code=404, detail="valid cached audio track not found")
    metadata = audio.metadata(hid)
    playbacks.register(hid, token, metadata, cached_audio=True, request_metadata=body)
    return {
        "url": _audio_url(request, hid, token),
        "cached": True,
        "hid": hid,
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
    }


def _audio_response(path: Path, media_type: str, cache_control: str = "no-cache",
                    debug_headers: dict | None = None):
    return FileResponse(path, media_type=media_type, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
        **(debug_headers or {}),
    })


def _audio_debug_headers(control: dict, offset: float) -> dict:
    return {
        "X-Sidecar-Offset": f"{offset:.6f}",
        "X-Sidecar-Offset-Source": str(control.get("source") or "request"),
        "X-Sidecar-Revision": str(control.get("revision") or 0),
    }


@app.get("/dual/aud/{hid}/audio.m3u8")
async def audio_playlist(hid: str, request: Request, o: int = 0, r: int = 1_000_000_000):
    token = _require_session(request)
    try:
        metadata = audio.metadata(hid)
        offset, rate, control = playbacks.resolve(
            hid, token, o / 1000.0, r / 1_000_000_000, "playlist"
        )
        effective_o = int(round(offset * 1000))
        effective_r = int(round(rate * 1_000_000_000))
        timeline = audio.timeline(metadata, offset, rate)
        if not timeline:
            raise ValueError("empty audio timeline")
        base = _base_url(request)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
                 "#EXT-X-MEDIA-SEQUENCE:0",
                 f'#EXT-X-MAP:URI="{base}/dual/aud/{hid}/init.mp4?{urlencode({"o": effective_o, "r": effective_r, "t": token})}"']
        for item in timeline:
            query = urlencode({"o": effective_o, "r": effective_r, "t": token})
            lines += [f"#EXTINF:{item['duration']:.6f},", f"{base}/dual/aud/{hid}/s{item['idx']}.m4s?{query}"]
        lines.append("#EXT-X-ENDLIST")
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache",
                                 **_audio_debug_headers(control, offset)})
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/dual/aud/{hid}/init.mp4")
async def audio_init(hid: str, request: Request, o: int = 0, r: int = 1_000_000_000):
    token = _require_session(request)
    try:
        metadata = audio.metadata(hid)
        offset, rate, control = playbacks.resolve(
            hid, token, o / 1000.0, r / 1_000_000_000, "init"
        )
        timeline = audio.timeline(metadata, offset, rate)
        if not timeline:
            raise ValueError("empty audio timeline")
        init_path, _ = await audio.fragment(hid, timeline[0]["idx"], offset, rate)
        return _audio_response(init_path, "video/mp4", "no-cache",
                               _audio_debug_headers(control, offset))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/dual/aud/{hid}/s{idx}.m4s")
async def audio_segment(hid: str, idx: int, request: Request, o: int = 0, r: int = 1_000_000_000):
    token = _require_session(request)
    try:
        offset, rate, control = playbacks.resolve(
            hid, token, o / 1000.0, r / 1_000_000_000, "segment"
        )
        _, fragment_path = await audio.fragment(hid, idx, offset, rate)
        return _audio_response(fragment_path, "video/iso.segment", "no-cache",
                               _audio_debug_headers(control, offset))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/offset/lookup")
async def offset_lookup(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    result = await offsets.lookup(body)
    cache_source = result.get("_cache_source") if isinstance(result, dict) else None
    public_result = dict(result) if isinstance(result, dict) else result
    if isinstance(public_result, dict):
        public_result.pop("_cache_source", None)
    hid = str(body.get("audio_hid") or body.get("hid") or "")
    if hid and result:
        details = dict(result.get("details") or result)
        details.pop("_cache_source", None)
        details.update({
            "cached": True,
            "cache_source": cache_source or "unknown",
            "cache_key": body.get("cache_key"),
        })
        playbacks.bind(hid, token, body, details)
    return {
        "found": bool(result),
        "offset": public_result,
        "cached": bool(result),
        "cache_source": cache_source,
    }


@app.post("/offset/report")
async def offset_report(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    result = body.get("offset")
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="offset result required")
    report_status = await offsets.report(
        body, result, upload_remote=offsets.automatic_upload_enabled
    )
    hid = str(body.get("audio_hid") or body.get("hid") or "")
    if hid:
        playbacks.bind(hid, token, body, result)
    return {"ok": True, "storage": report_status}


@app.post("/sync")
async def sync_audio(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    try:
        result = await sync_engine.measure(body)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        hid = str(body.get("audio_hid") or "")
        if hid:
            playbacks.bind(hid, token, body, {
                "status": "error",
                "cached": False,
                "cache_source": None,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            })
        raise HTTPException(status_code=422, detail=str(exc))
    report_status = await offsets.report(
        body, result, upload_remote=offsets.automatic_upload_enabled
    )
    result["storage"] = report_status
    hid = str(body.get("audio_hid") or "")
    if hid:
        playbacks.bind(hid, token, body, result)
    if result.get("status") != "ok":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EDITIONS_DIFFERENT",
                "message": "Edizioni audio e video differenti: sincronizzazione affidabile impossibile.",
            },
        )
    return result


async def _dashboard_snapshot(include_activity: bool = True, include_tracks: bool = True,
                              include_offsets: bool = True):
    state = playbacks.snapshot()
    if include_tracks:
        inspected = {}
        for player in state["players"]:
            hid = player.get("hid")
            if not hid:
                continue
            if hid not in inspected:
                try:
                    inspected[hid] = audio.inspect(hid)
                except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
                    inspected[hid] = {"hid": hid, "error": f"{type(exc).__name__}: {exc}"}
            player["audio_track"] = inspected[hid]
    offset_records = await offsets.list() if include_offsets else None
    dynamic_hosts = sorted({
        str(
            (player.get("sync_metadata") or {}).get("vpsHost")
            or (player.get("sync_metadata") or {}).get("vps_host")
            or (player.get("prepare_request") or {}).get("vpsHost")
            or (player.get("prepare_request") or {}).get("vps_host")
        ).rstrip("/")
        for player in state["players"]
        if (
            (player.get("sync_metadata") or {}).get("vpsHost")
            or (player.get("sync_metadata") or {}).get("vps_host")
            or (player.get("prepare_request") or {}).get("vpsHost")
            or (player.get("prepare_request") or {}).get("vps_host")
        )
    })
    if offset_records is not None:
        for record in offset_records:
            stored_context = (record.get("details") or {}).get("remote_context") or {}
            record["upload_context"] = {
                **stored_context,
                **{key: value for key, value in playbacks.context_for_cache(record["cache_key"]).items() if value},
            }
    state.update({
        "server": {
            "service": "toast-audio-sidecar",
            "version": app.version,
            "server_time": time.time(),
            "public_url": PUBLIC_BASE_URL or None,
            "cache_dir": str(CACHE_DIR),
            "remote_db_configured": bool(OFFSET_API_URL),
            "configured_offset_api_url": OFFSET_API_URL or None,
            "dynamic_vps_hosts": dynamic_hosts,
            "automatic_remote_upload_enabled": offsets.automatic_upload_enabled,
            "audio_proxy_configured": bool(AUDIO_PROXY),
            "session_ttl_seconds": SESSION_TTL,
            "activity_max_rows": activity.max_rows,
        },
    })
    if offset_records is not None:
        state["offsets"] = offset_records
    if include_activity:
        state["activity"] = await activity.recent(400)
    return state


@app.get("/api/dashboard/state", include_in_schema=False)
async def dashboard_state(request: Request):
    _require_admin(request)
    return JSONResponse(
        await _dashboard_snapshot(include_activity=True),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/dashboard/events", include_in_schema=False)
async def dashboard_events(request: Request):
    _require_admin(request)

    async def stream():
        queue = activity.subscribe()
        try:
            initial = {"type": "snapshot", "state": await _dashboard_snapshot(True, True, True)}
            yield f"event: snapshot\ndata: {json.dumps(initial, ensure_ascii=False, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    first = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                changes = [first]
                await asyncio.sleep(0.12)
                while not queue.empty() and len(changes) < 100:
                    changes.append(queue.get_nowait())
                urls = [str(change.get("url") or "") for change in changes]
                include_tracks = any(
                    "/dual/aprep" in url or "/dual/acache" in url or "/sync" in url
                    for url in urls
                )
                include_offsets = any(
                    marker in url
                    for url in urls
                    for marker in ("/sync", "/offset/report", "/api/dashboard/offsets", "/api/dashboard/players")
                )
                update = {
                    "type": "update",
                    "state": await _dashboard_snapshot(False, include_tracks, include_offsets),
                    "activity": changes,
                }
                yield f"event: update\ndata: {json.dumps(update, ensure_ascii=False, default=str)}\n\n"
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.get("/api/dashboard/activity/{activity_id}", include_in_schema=False)
async def dashboard_activity_detail(activity_id: int, request: Request):
    _require_admin(request)
    detail = await activity.get(activity_id)
    if not detail:
        raise HTTPException(status_code=404, detail="activity record not found")
    return JSONResponse(detail, headers={"Cache-Control": "no-store"})


@app.patch("/api/dashboard/settings/automatic-upload", include_in_schema=False)
async def dashboard_automatic_upload_setting(request: Request):
    _require_admin(request)
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    saved = await offsets.set_automatic_upload_enabled(enabled)
    playbacks.note("automatic-upload-setting", enabled=saved)
    return {
        "ok": True,
        "enabled": saved,
        "message": (
            "Automatic remote uploads enabled."
            if saved else
            "Automatic remote uploads disabled; sync results will remain local until an admin uploads one."
        ),
    }


@app.get("/api/dashboard/audio/{hid}/segments/{idx}/preview.wav", include_in_schema=False)
async def dashboard_audio_preview(hid: str, idx: int, request: Request):
    _require_admin(request)
    try:
        path = await audio.preview(hid, idx)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(path, media_type="audio/wav", filename=f"{hid}-segment-{idx}.wav", headers={
        "Cache-Control": "private, max-age=3600",
        "Accept-Ranges": "bytes",
    })


@app.get("/api/dashboard/players/{playback_id}/alignment/{kind}.wav", include_in_schema=False)
async def dashboard_alignment_preview(playback_id: str, kind: str, request: Request,
                                      position: float = 60.0, seconds: float = 8.0):
    _require_admin(request)
    player = playbacks.get(playback_id)
    if not player:
        raise HTTPException(status_code=404, detail="playback not found")
    payload = {
        **(player.get("prepare_request") or {}),
        **(player.get("sync_metadata") or {}),
    }
    payload["video_url"] = (
        payload.get("video_url") or payload.get("videoUrl") or payload.get("videoURL") or ""
    )
    payload["video_headers"] = payload.get("video_headers") or payload.get("videoHeaders") or {}
    payload["reference_audio_url"] = (
        payload.get("reference_audio_url") or payload.get("referenceAudioUrl")
        or payload.get("referenceAudio") or ""
    )
    payload["audio_hid"] = player["hid"]
    try:
        path = await sync_engine.manual_preview(payload, kind, position, seconds)
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(
        path, media_type="audio/wav",
        filename=f"{playback_id}-{kind}-{position:.3f}.wav",
        headers={"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"},
    )


@app.patch("/api/dashboard/players/{playback_id}/offset", include_in_schema=False)
async def dashboard_edit_player(playback_id: str, request: Request):
    _require_admin(request)
    body = await request.json()
    offset, rate = _offset_values(body)
    player = playbacks.get(playback_id)
    if not player:
        raise HTTPException(status_code=404, detail="active playback not found")
    record = None
    if player.get("cache_key"):
        try:
            metadata = dict(player.get("sync_metadata") or {})
            candidates = player.get("offset_candidates") or {}
            automatic = candidates.get("calculated") or candidates.get("cached") or candidates.get("request")
            if automatic:
                metadata["_automatic_result"] = dict(automatic.get("result") or automatic)
            record = await offsets.update_custom(
                player["cache_key"], offset, rate, str(body.get("note") or ""),
                metadata,
            )
        except KeyError:
            record = None
    updated = playbacks.set_override(playback_id, offset, rate)
    return {
        "ok": True,
        "player": updated,
        "record": record,
        "persisted": record is not None,
        "message": (
            "Saved locally and queued for the next player request."
            if record else
            "Applied live in memory. Save it again after sync exposes a cache key to persist it."
        ),
    }


@app.post("/api/dashboard/players/{playback_id}/offset/restore", include_in_schema=False)
async def dashboard_restore_player(playback_id: str, request: Request,
                                   source: str | None = None):
    _require_admin(request)
    player = playbacks.get(playback_id)
    if not player:
        raise HTTPException(status_code=404, detail="active playback not found")
    try:
        updated = playbacks.restore_automatic(playback_id, source)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record = None
    if player.get("cache_key"):
        try:
            selected = (player.get("offset_candidates") or {}).get(updated["control_source"]) or {}
            value = dict(selected.get("result") or selected)
            record = await offsets.restore_automatic(
                player["cache_key"], value, updated["control_source"]
            )
        except KeyError:
            record = None
    return {
        "ok": True,
        "player": updated,
        "record": record,
        "message": f"Restored the {updated['control_source']} offset and queued it for the next player request.",
    }


@app.patch("/api/dashboard/offsets/{cache_key}", include_in_schema=False)
async def dashboard_edit_offset(cache_key: str, request: Request):
    _require_admin(request)
    body = await request.json()
    offset, rate = _offset_values(body)
    try:
        record = await offsets.update_custom(cache_key, offset, rate, str(body.get("note") or ""))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    active_players = playbacks.set_override_for_cache(cache_key, offset, rate)
    return {
        "ok": True,
        "record": record,
        "active_players_updated": active_players,
        "message": (
            f"Saved locally and queued on {active_players} active player(s)."
            if active_players else
            "Saved locally; it will be returned on the next matching player lookup."
        ),
    }


@app.post("/api/dashboard/offsets/{cache_key}/restore", include_in_schema=False)
async def dashboard_restore_offset(cache_key: str, request: Request):
    _require_admin(request)
    try:
        record = await offsets.restore_automatic(cache_key)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    active_players = playbacks.restore_for_cache(cache_key)
    return {
        "ok": True,
        "record": record,
        "active_players_updated": active_players,
        "message": "Restored the original automatic offset.",
    }


@app.post("/api/dashboard/offsets/{cache_key}/upload", include_in_schema=False)
async def dashboard_upload_offset(cache_key: str, request: Request,
                                  playback_id: str | None = None):
    _require_admin(request)
    selected_result = None
    if playback_id:
        player = playbacks.get(playback_id)
        if not player:
            raise HTTPException(status_code=404, detail="playback not found")
        if player.get("cache_key") != cache_key:
            raise HTTPException(status_code=409, detail="playback does not use this offset record")
        source = str(player.get("control_source") or "request")
        candidate = (player.get("offset_candidates") or {}).get(source) or {}
        selected_result = dict(candidate.get("result") or candidate)
        selected_result.update({
            "status": "ok",
            "offset": float(player.get("current_offset") or 0.0),
            "rate": float(player.get("current_rate") or 1.0),
            "cached": source == "cached",
            "custom": source == "manual",
            "selected_for_upload": source,
        })
    try:
        context = playbacks.context_for_cache(cache_key)
        status = await offsets.upload(cache_key, context, selected_result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    playbacks.note(
        "remote-upload", cache_key=cache_key, playback_id=playback_id,
        selected_source=(selected_result or {}).get("selected_for_upload"), status=status,
    )
    if not status.get("remote_uploaded"):
        raise HTTPException(status_code=502, detail=status)
    return {"ok": True, "storage": status}
