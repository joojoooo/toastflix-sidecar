import base64
import hashlib
import time
from collections import deque


PLAYBACK_ACTIVE_GRACE_SECONDS = 5 * 60


def safe_metadata(value, key: str = ""):
    """Make captured metadata JSON-compatible without removing any fields."""
    if isinstance(value, dict):
        return {str(name): safe_metadata(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_metadata(item) for item in value]
    if isinstance(value, bytes):
        return {"encoding": "base64", "content": base64.b64encode(value).decode("ascii")}
    return value


def audio_debug_metadata(metadata: dict) -> dict:
    segments = metadata.get("segs") or []
    durations = metadata.get("durs") or []
    result = safe_metadata(metadata)
    result["segment_summary"] = {
        "count": len(segments),
        "duration_seconds": round(sum(float(value) for value in durations), 3),
        "first_url": segments[0] if segments else None,
        "last_url": segments[-1] if segments else None,
    }
    return result


class PlaybackRegistry:
    """In-memory live controls for the single-process HLS sidecar."""

    def __init__(self):
        self._players: dict[str, dict] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self._events = deque(maxlen=150)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @classmethod
    def _playback_id(cls, hid: str, token: str) -> str:
        return hashlib.sha256(f"{cls._token_hash(token)}:{hid}".encode()).hexdigest()[:16]

    def _event(self, kind: str, playback_id: str = "", **details):
        self._events.appendleft({
            "at": time.time(),
            "kind": kind,
            "playback_id": playback_id or None,
            **safe_metadata(details),
        })

    def note(self, kind: str, **details):
        self._event(kind, **details)

    def register(self, hid: str, token: str, metadata: dict, cached_audio: bool = False,
                 request_metadata: dict | None = None) -> str:
        now = time.time()
        playback_id = self._playback_id(hid, token)
        token_hash = self._token_hash(token)
        media_key = str(metadata.get("media_key") or "")
        if media_key:
            for existing in self._players.values():
                existing_media = str((existing.get("audio_metadata") or {}).get("media_key") or "")
                if existing["token_hash"] == token_hash and existing_media and existing_media != media_key:
                    existing["ended_at"] = now
        player = self._players.get(playback_id)
        if player is None:
            player = {
                "playback_id": playback_id,
                "session_id": token_hash[:12],
                "hid": hid,
                "token_hash": token_hash,
                "created_at": now,
                "ended_at": None,
                "cache_key": None,
                "resolution": None,
                "current_offset": 0.0,
                "current_rate": 1.0,
                "control_source": "request",
                "revision": 0,
                "applied_revision": 0,
                "request_count": 0,
                "last_request_at": None,
                "last_request_kind": None,
                "last_requested_offset": 0.0,
                "last_requested_rate": 1.0,
                "cache_hit": None,
                "cache_source": None,
                "sync_result": None,
                "sync_metadata": None,
                "offset_candidates": {
                    "request": {
                        "offset": 0.0,
                        "rate": 1.0,
                        "source": "sidecar initial zero default",
                    },
                },
            }
            self._players[playback_id] = player
            self._keys[(token_hash, hid)] = playback_id
        player.update({
            "updated_at": now,
            "ended_at": None,
            "cached_audio": bool(cached_audio),
            "audio_metadata": audio_debug_metadata(metadata),
            "prepare_request": safe_metadata(request_metadata or {}),
        })
        self._event("audio-cache" if cached_audio else "audio-prepared", playback_id, hid=hid)
        self._prune()
        return playback_id

    def bind(self, hid: str, token: str, payload: dict, result: dict | None) -> str:
        playback_id = self._playback_id(hid, token)
        if playback_id not in self._players:
            self.register(hid, token, {}, False)
        player = self._players[playback_id]
        result = result if isinstance(result, dict) else {}
        cache_key = str(payload.get("cache_key") or result.get("cache_key") or "") or None
        cache_source = result.get("cache_source") or result.get("cached_source")
        cache_hit = bool(result.get("cached")) if "cached" in result else None
        player.update({
            "updated_at": time.time(),
            "cache_key": player.get("metadata_edits", {}).get("cache_key", cache_key),
            "resolution": player.get("metadata_edits", {}).get("resolution", payload.get("resolution")),
            "cache_hit": cache_hit,
            "cache_source": cache_source,
            "sync_result": safe_metadata(result),
            "sync_metadata": {
                **safe_metadata(payload),
                **player.get("metadata_edits", {}),
            },
        })
        if result.get("status", "ok") == "ok" and isinstance(result.get("offset"), (int, float)):
            candidate = {
                "offset": float(result["offset"]),
                "rate": float(result.get("rate", 1.0)),
                "confidence": result.get("confidence"),
                "source": cache_source or ("database" if result.get("cached") else "sidecar calculation"),
                "result": safe_metadata(result),
            }
            is_custom = bool(result.get("custom"))
            automatic = result.get("automatic_result")
            if is_custom:
                player["offset_candidates"]["manual"] = candidate
                if isinstance(automatic, dict) and isinstance(automatic.get("offset"), (int, float)):
                    player["offset_candidates"]["cached"] = {
                        "offset": float(automatic["offset"]),
                        "rate": float(automatic.get("rate", 1.0)),
                        "confidence": automatic.get("confidence"),
                        "source": "original automatic value",
                        "result": safe_metadata(automatic),
                    }
                selected_source = "manual"
            else:
                selected_source = "cached" if result.get("cached") else "calculated"
                player["offset_candidates"][selected_source] = candidate
            if player["control_source"] != "manual" or is_custom:
                player["current_offset"] = candidate["offset"]
                player["current_rate"] = candidate["rate"]
                player["control_source"] = selected_source
                player["revision"] += 1
        event_kind = (
            "offset-cache-hit" if result.get("cached")
            else "offset-cache-miss" if result.get("status") == "lookup-miss"
            else "sync-complete" if result.get("status", "ok") == "ok"
            else "sync-error"
        )
        self._event(
            event_kind,
            playback_id,
            cache_key=cache_key,
            cache_source=cache_source,
            status=result.get("status"),
        )
        return playback_id

    def resolve(self, hid: str, token: str, requested_offset: float, requested_rate: float,
                request_kind: str) -> tuple[float, float, dict]:
        playback_id = self._keys.get((self._token_hash(token), hid))
        player = self._players.get(playback_id or "")
        if not player:
            return requested_offset, requested_rate, {
                "playback_id": None,
                "source": "request",
                "revision": 0,
            }
        if player["control_source"] == "request":
            player["current_offset"] = float(requested_offset)
            player["current_rate"] = float(requested_rate)
            player["offset_candidates"]["request"] = {
                "offset": float(requested_offset),
                "rate": float(requested_rate),
                "source": "value carried by incoming HLS request",
            }
        player["last_requested_offset"] = float(requested_offset)
        player["last_requested_rate"] = float(requested_rate)
        player["last_request_at"] = time.time()
        player["last_request_kind"] = request_kind
        player["request_count"] += 1
        player["updated_at"] = time.time()
        player["applied_revision"] = player["revision"]
        if request_kind == "playlist" or player["request_count"] % 10 == 0:
            self._event(
                "player-request",
                player["playback_id"],
                request_kind=request_kind,
                effective_offset=player["current_offset"],
                revision=player["revision"],
            )
        return player["current_offset"], player["current_rate"], {
            "playback_id": player["playback_id"],
            "source": player["control_source"],
            "revision": player["revision"],
        }

    def set_override(self, playback_id: str, offset: float, rate: float) -> dict:
        player = self._players.get(playback_id)
        if not player:
            raise KeyError("active playback not found")
        player.update({
            "current_offset": float(offset),
            "current_rate": float(rate),
            "control_source": "manual",
            "updated_at": time.time(),
            "revision": player["revision"] + 1,
        })
        player["offset_candidates"]["manual"] = {
            "offset": float(offset), "rate": float(rate), "source": "dashboard edit",
        }
        self._event(
            "manual-offset-saved", playback_id,
            cache_key=player.get("cache_key"), offset=offset, rate=rate,
        )
        return self._public_player(player)

    def set_override_for_cache(self, cache_key: str, offset: float, rate: float) -> int:
        changed = 0
        for player in self._players.values():
            if player.get("cache_key") != cache_key:
                continue
            self.set_override(player["playback_id"], offset, rate)
            changed += 1
        return changed

    def attach_cache_key(self, playback_id: str, cache_key: str, metadata: dict) -> dict:
        """Attach a dashboard-created offset identity to an existing playback."""
        player = self._players.get(playback_id)
        if not player:
            raise KeyError("active playback not found")
        player["cache_key"] = str(cache_key)
        if metadata.get("resolution") is not None:
            player["resolution"] = metadata["resolution"]
        player["sync_metadata"] = {
            **(player.get("sync_metadata") or {}),
            **safe_metadata(metadata),
        }
        player["updated_at"] = time.time()
        self._event("offset-cache-attached", playback_id, cache_key=cache_key)
        return self._public_player(player)

    def set_metadata_field(self, playback_id: str, field: str, value: str | int | dict) -> dict:
        """Keep dashboard-supplied track metadata through later sync updates."""
        player = self._players.get(playback_id)
        if not player:
            raise KeyError("playback not found")
        edits = player.setdefault("metadata_edits", {})
        edits[field] = safe_metadata(value)
        player["sync_metadata"] = {
            **(player.get("sync_metadata") or {}),
            field: edits[field],
        }
        if field in {"cache_key", "resolution"}:
            player[field] = value
        player["updated_at"] = time.time()
        self._event("playback-metadata-edited", playback_id, field=field)
        return self._public_player(player)

    def restore_automatic(self, playback_id: str, requested_source: str | None = None) -> dict:
        player = self._players.get(playback_id)
        if not player:
            raise KeyError("active playback not found")
        candidates = player.get("offset_candidates") or {}
        if requested_source:
            if requested_source not in {"calculated", "cached", "request"}:
                raise KeyError("automatic offset source must be calculated, cached, or request")
            if requested_source not in candidates:
                raise KeyError(f"{requested_source} offset is not available for this playback")
            source = requested_source
        else:
            source = next(
                (name for name in ("calculated", "cached", "request") if name in candidates),
                None,
            )
        if not source:
            raise KeyError("no automatic offset is available for this playback")
        candidate = candidates[source]
        player.update({
            "current_offset": float(candidate["offset"]),
            "current_rate": float(candidate.get("rate", 1.0)),
            "control_source": source,
            "updated_at": time.time(),
            "revision": player["revision"] + 1,
        })
        player["offset_candidates"].pop("manual", None)
        self._event(
            "manual-offset-restored", playback_id, cache_key=player.get("cache_key"),
            offset=player["current_offset"], source=source,
        )
        return self._public_player(player)

    def context_for_cache(self, cache_key: str) -> dict:
        matches = [player for player in self._players.values() if player.get("cache_key") == cache_key]
        latest = max(matches, key=lambda player: player.get("updated_at") or 0) if matches else None
        return self._context_with_fallback(latest)

    def context_for_playback(self, playback_id: str) -> dict:
        return self._context_with_fallback(self._players.get(playback_id))

    def _context_with_fallback(self, player: dict | None) -> dict:
        context = self._context_from_player(player)
        fallback = self.latest_remote_context()
        if ((not context.get("vpsHost") or not context.get("vpsAccess"))
                and fallback.get("vpsHost") and fallback.get("vpsAccess")):
            context["vpsHost"] = fallback["vpsHost"]
            context["vpsAccess"] = fallback["vpsAccess"]
        elif not context.get("vpsHost") and fallback.get("vpsHost"):
            context["vpsHost"] = fallback["vpsHost"]
            context["vpsAccess"] = fallback.get("vpsAccess") or ""
        for key, value in fallback.items():
            if key in ("vpsHost", "vpsAccess"):
                continue
            if not context.get(key) and value:
                context[key] = value
        return context

    @staticmethod
    def _context_from_player(player: dict | None) -> dict:
        if not player:
            return {}
        prepare = player.get("prepare_request") or {}
        sync = player.get("sync_metadata") or {}
        metadata = {**prepare, **sync}
        contexts = [
            {
                "vpsHost": source.get("vpsHost") or source.get("vps_host") or "",
                "vpsAccess": source.get("vpsAccess") or source.get("vps_access") or "",
            }
            for source in (sync, prepare)
        ]
        connection = next(
            (item for item in contexts if item["vpsHost"] and item["vpsAccess"]),
            next((item for item in contexts if item["vpsHost"]), contexts[0]),
        )
        return {
            **connection,
            "provider": metadata.get("provider") or "",
            "server": metadata.get("server") or "",
            "title": metadata.get("title") or "",
        }

    def latest_remote_context(self) -> dict:
        """Return the newest reusable VPS context, filling gaps from older tracks."""
        players = sorted(
            self._players.values(),
            key=lambda player: player.get("updated_at") or 0,
            reverse=True,
        )
        candidates = [self._context_from_player(player) for player in players]
        complete = next(
            (item for item in candidates if item.get("vpsHost") and item.get("vpsAccess")),
            None,
        )
        if complete:
            return complete
        context = {"vpsHost": "", "vpsAccess": ""}
        for candidate in candidates:
            for key, value in candidate.items():
                if key in ("vpsHost", "vpsAccess"):
                    continue
                if not context.get(key) and value:
                    context[key] = value
        partial = next((item for item in candidates if item.get("vpsHost")), None)
        if partial:
            context["vpsHost"] = partial["vpsHost"]
            context["vpsAccess"] = partial.get("vpsAccess") or ""
        return context

    def restore_for_cache(self, cache_key: str) -> int:
        restored = 0
        for player in list(self._players.values()):
            if player.get("cache_key") != cache_key:
                continue
            try:
                self.restore_automatic(player["playback_id"])
                restored += 1
            except KeyError:
                continue
        return restored

    @staticmethod
    def _public_player(player: dict) -> dict:
        result = {key: value for key, value in player.items() if key != "token_hash"}
        result["pending_player_request"] = player["revision"] > player["applied_revision"]
        last_request = player.get("last_request_at") or 0
        recently_updated = (
            time.time() - (player.get("updated_at") or 0) < PLAYBACK_ACTIVE_GRACE_SECONDS
        )
        result["active"] = not player.get("ended_at") and (
            time.time() - last_request < PLAYBACK_ACTIVE_GRACE_SECONDS
            if last_request else recently_updated
        )
        return result

    def get(self, playback_id: str) -> dict | None:
        player = self._players.get(playback_id)
        return self._public_player(player) if player else None

    def snapshot(self) -> dict:
        self._prune()
        players = sorted(
            (self._public_player(player) for player in self._players.values()),
            key=lambda item: item.get("updated_at") or 0,
            reverse=True,
        )
        return {"players": players, "events": list(self._events)}

    def _prune(self):
        cutoff = time.time() - 24 * 3600
        stale = [key for key, player in self._players.items()
                 if (player.get("updated_at") or 0) < cutoff]
        for playback_id in stale:
            player = self._players.pop(playback_id)
            self._keys.pop((player["token_hash"], player["hid"]), None)
