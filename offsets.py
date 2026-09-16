import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

from activity import logged_http_request


class OffsetStore:
    """Local offset cache with an optional central VPS lookup/report endpoint."""

    REMOTE_FIELDS = (
        "cache_key",
        "media_key",
        "resolution",
        "video_fingerprint",
        "audio_fingerprint",
        "provider",
        "server",
        "title",
    )

    def __init__(self, path: str, api_url: str = "", api_token: str = "", activity=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token.strip()
        self.activity = activity
        self._init_db()
        self._automatic_upload_enabled = self._load_automatic_upload_enabled()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS offsets (
                    cache_key TEXT PRIMARY KEY,
                    media_key TEXT NOT NULL,
                    resolution INTEGER NOT NULL,
                    video_fingerprint TEXT NOT NULL,
                    audio_fingerprint TEXT NOT NULL,
                    offset_seconds REAL,
                    rate REAL NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

    def _load_automatic_upload_enabled(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("automatic_remote_upload",),
            ).fetchone()
        return True if not row else str(row[0]).strip().lower() == "true"

    @property
    def automatic_upload_enabled(self) -> bool:
        return self._automatic_upload_enabled

    def _save_automatic_upload_enabled(self, enabled: bool):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)""",
                ("automatic_remote_upload", "true" if enabled else "false", time.time()),
            )

    async def set_automatic_upload_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        await asyncio.to_thread(self._save_automatic_upload_enabled, enabled)
        self._automatic_upload_enabled = enabled
        return enabled

    @staticmethod
    def key(media_key: str, resolution: int, video_fp: str, audio_fp: str) -> str:
        import hashlib
        return hashlib.sha1(
            f"v2|{media_key}|{resolution}|{video_fp}|{audio_fp}".encode()
        ).hexdigest()

    def _local_get(self, cache_key: str):
        with self._connect() as conn:
            columns = [item[1] for item in conn.execute("PRAGMA table_info(offsets)")]
            row = conn.execute(
                "SELECT * FROM offsets WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if not row:
            return None
        result = dict(zip(columns, row))
        result["details"] = json.loads(result.get("details") or "{}")
        return result

    def _local_list(self, limit: int = 200):
        with self._connect() as conn:
            columns = [item[1] for item in conn.execute("PRAGMA table_info(offsets)")]
            rows = conn.execute(
                "SELECT * FROM offsets ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["details"] = json.loads(item.get("details") or "{}")
            result.append(item)
        return result

    async def get(self, cache_key: str):
        return await asyncio.to_thread(self._local_get, cache_key)

    async def list(self, limit: int = 200):
        return await asyncio.to_thread(self._local_list, limit)

    @classmethod
    def _remote_payload(cls, payload: dict) -> dict:
        result = {
            name: payload[name]
            for name in cls.REMOTE_FIELDS
            if name in payload and payload[name] not in (None, "")
        }
        dynamic_access = payload.get("vpsAccess") or payload.get("vps_access")
        if dynamic_access:
            result["access"] = dynamic_access
        return result

    @staticmethod
    def _remote_context(payload: dict) -> dict:
        result = {
            name: payload.get(name)
            for name in ("vpsHost", "vpsAccess", "provider", "server", "title")
            if payload.get(name) not in (None, "")
        }
        if not result.get("vpsHost") and payload.get("vps_host"):
            result["vpsHost"] = payload["vps_host"]
        if not result.get("vpsAccess") and payload.get("vps_access"):
            result["vpsAccess"] = payload["vps_access"]
        return result

    @classmethod
    def _remote_offset_result(cls, value):
        """Keep local VPS credentials out of the offset object sent to the VPS."""
        if isinstance(value, dict):
            return {
                name: cls._remote_offset_result(item)
                for name, item in value.items() if name != "remote_context"
            }
        if isinstance(value, list):
            return [cls._remote_offset_result(item) for item in value]
        return value

    async def lookup(self, payload: dict):
        local_result = await asyncio.to_thread(self._local_get, payload["cache_key"])
        if local_result and (local_result.get("details") or {}).get("custom"):
            local_result["_cache_source"] = "local-custom"
            return local_result
        api_url = self.api_url
        dynamic_host = payload.get("vpsHost") or payload.get("vps_host")
        if not api_url and dynamic_host:
            api_url = f"{str(dynamic_host).rstrip('/')}/dual/offset"
        if api_url:
            try:
                headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
                central_payload = self._remote_payload(payload)
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await logged_http_request(
                        self.activity, client, "POST", f"{api_url}/lookup", "offset-db",
                        json=central_payload, headers=headers,
                    )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("found") and data.get("offset") is not None:
                        result = data["offset"]
                        if not isinstance(result, dict):
                            result = {"status": "ok", "offset": float(result), "rate": 1.0}
                        cache_value = dict(result.get("details") or result)
                        context = self._remote_context(payload)
                        if context:
                            cache_value["remote_context"] = context
                        if all(name in payload for name in (
                            "cache_key", "media_key", "resolution",
                            "video_fingerprint", "audio_fingerprint",
                        )):
                            await asyncio.to_thread(self._local_put, payload, cache_value)
                        result = {**result, "_cache_source": "remote"}
                        return result
            except Exception as exc:
                print(f"[sidecar offsets] remote lookup failed: {type(exc).__name__}")
                pass
        result = local_result
        if result:
            result["_cache_source"] = "local"
        return result

    def _local_put(self, payload: dict, result: dict):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO offsets
                (cache_key, media_key, resolution, video_fingerprint, audio_fingerprint,
                 offset_seconds, rate, confidence, status, details, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["cache_key"], payload["media_key"], payload["resolution"],
                    payload["video_fingerprint"], payload["audio_fingerprint"],
                    result.get("offset"), result.get("rate", 1.0),
                    result.get("confidence", 0.0), result.get("status", "incompatible"),
                    json.dumps(result, separators=(",", ":")), time.time(),
                ),
            )

    async def report(self, payload: dict, result: dict, upload_remote: bool = True):
        local_result = dict(result)
        context = self._remote_context(payload)
        if context:
            local_result["remote_context"] = context
        await asyncio.to_thread(self._local_put, payload, local_result)
        api_url = self.api_url
        dynamic_host = payload.get("vpsHost") or payload.get("vps_host")
        if not api_url and dynamic_host:
            api_url = f"{str(dynamic_host).rstrip('/')}/dual/offset"
        if not upload_remote:
            return {
                "local_saved": True,
                "remote_configured": bool(api_url),
                "remote_uploaded": False,
                "remote_skipped": True,
                "remote_skip_reason": "automatic remote uploads are disabled by the administrator",
            }
        if not api_url:
            return {"local_saved": True, "remote_configured": False, "remote_uploaded": False}
        try:
            headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
            central_payload = {
                **self._remote_payload(payload), "offset": self._remote_offset_result(result),
            }
            async with httpx.AsyncClient(timeout=8) as client:
                response = await logged_http_request(
                    self.activity, client, "POST", f"{api_url}/report", "offset-db",
                    json=central_payload,
                    headers=headers,
                )
            response.raise_for_status()
            return {
                "local_saved": True,
                "remote_configured": True,
                "remote_uploaded": True,
                "remote_status": response.status_code,
            }
        except Exception as exc:
            print(f"[sidecar offsets] remote report failed: {type(exc).__name__}")
            return {
                "local_saved": True,
                "remote_configured": True,
                "remote_uploaded": False,
                "remote_error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }

    async def update_custom(self, cache_key: str, offset: float, rate: float,
                            note: str = "", metadata: dict | None = None):
        record = await self.get(cache_key)
        if not record and not metadata:
            raise KeyError("offset record not found")
        details = dict(
            (record or {}).get("details")
            or ((metadata or {}).get("_automatic_result") or {})
        )
        if not details.get("custom"):
            details["automatic_result"] = dict(details)
        history = list(details.get("manual_history") or [])
        history.append({
            "offset": float(offset), "rate": float(rate), "note": str(note or "")[:500],
            "updated_at": time.time(),
        })
        details.update({
            "status": "ok",
            "offset": float(offset),
            "rate": float(rate),
            "confidence": float(details.get("confidence", (record or {}).get("confidence") or 0.0)),
            "custom": True,
            "custom_note": str(note or "")[:500],
            "custom_updated_at": time.time(),
            "manual_history": history[-50:],
        })
        source = record or metadata or {}
        required = ("media_key", "resolution", "video_fingerprint", "audio_fingerprint")
        if any(name not in source for name in required):
            raise KeyError("playback does not have enough metadata to persist this offset")
        payload = {"cache_key": cache_key, **{name: source[name] for name in required}}
        await asyncio.to_thread(self._local_put, payload, details)
        return await self.get(cache_key)

    async def restore_automatic(self, cache_key: str, value: dict | None = None,
                                source: str = "automatic"):
        record = await self.get(cache_key)
        if not record:
            raise KeyError("offset record not found")
        details = dict(record.get("details") or {})
        automatic = value or details.get("automatic_result")
        if not isinstance(automatic, dict) or not isinstance(automatic.get("offset"), (int, float)):
            raise KeyError("this record has no previous automatic offset")
        restored = dict(automatic)
        restored.setdefault("status", "ok")
        restored.setdefault("rate", 1.0)
        restored.setdefault("confidence", float(record.get("confidence") or 0.0))
        restored.update({
            "custom": False,
            "restored_at": time.time(),
            "restored_source": str(source or "automatic"),
            "manual_history": details.get("manual_history") or [],
        })
        if details.get("remote_context") and not restored.get("remote_context"):
            restored["remote_context"] = details["remote_context"]
        payload = {
            "cache_key": record["cache_key"],
            "media_key": record["media_key"],
            "resolution": record["resolution"],
            "video_fingerprint": record["video_fingerprint"],
            "audio_fingerprint": record["audio_fingerprint"],
        }
        await asyncio.to_thread(self._local_put, payload, restored)
        return await self.get(cache_key)

    async def upload(self, cache_key: str, context: dict | None = None,
                     selected_result: dict | None = None):
        """Explicitly upload the selected local/player value, bypassing the automatic toggle."""
        record = await self.get(cache_key)
        if not record:
            raise KeyError("offset record not found")
        stored_context = (record.get("details") or {}).get("remote_context") or {}
        supplied = {
            key: value for key, value in (context or {}).items()
            if value not in (None, "")
        }
        context = {
            **stored_context,
            **supplied,
        }
        if (supplied.get("vpsHost")
                and supplied["vpsHost"].rstrip("/") != str(stored_context.get("vpsHost") or "").rstrip("/")
                and not supplied.get("vpsAccess")):
            context["vpsAccess"] = ""
        dynamic_host = str(context.get("vpsHost") or "").strip().rstrip("/")
        dynamic_access = str(context.get("vpsAccess") or "").strip()
        missing_fields = []
        if not self.api_url:
            if not dynamic_host:
                missing_fields.append("vpsHost")
            if not dynamic_access:
                missing_fields.append("vpsAccess")
        if missing_fields:
            return {
                "local_saved": True,
                "remote_configured": bool(dynamic_host),
                "remote_uploaded": False,
                "missing_fields": missing_fields,
                "remote_error": (
                    "Manual upload needs "
                    + " and ".join(missing_fields)
                    + " when OFFSET_API_URL is not configured"
                ),
            }
        payload = {
            "cache_key": record["cache_key"],
            "media_key": record["media_key"],
            "resolution": record["resolution"],
            "video_fingerprint": record["video_fingerprint"],
            "audio_fingerprint": record["audio_fingerprint"],
            **context,
        }
        result = dict(record.get("details") or {})
        if selected_result:
            result.update(selected_result)
        result["manually_uploaded_at"] = time.time()
        return await self.report(payload, result, upload_remote=True)

    async def upload_player_value(self, payload: dict, selected_result: dict):
        """Send a playback value first; persist it locally only after a successful upload.

        A canonical cache key can be attempted without the identity tuple. The
        tuple is needed only to create a local SQLite record after the VPS accepts it.
        """
        cache_key = str(payload.get("cache_key") or "").strip()
        if not cache_key:
            raise KeyError("offset cache key required")
        record = await self.get(cache_key)
        result = dict((record or {}).get("details") or {})
        if selected_result.get("custom"):
            if record and not result.get("custom"):
                result["automatic_result"] = dict(result)
            history = list(result.get("manual_history") or [])
            history.append({
                "offset": float(selected_result["offset"]),
                "rate": float(selected_result.get("rate", 1.0)),
                "note": "dashboard upload",
                "updated_at": time.time(),
            })
        result.update(selected_result)
        if selected_result.get("custom"):
            result["manual_history"] = history[-50:]
        result["manually_uploaded_at"] = time.time()
        context = self._remote_context(payload)
        if context:
            result["remote_context"] = context

        dynamic_host = str(payload.get("vpsHost") or "").strip().rstrip("/")
        dynamic_access = str(payload.get("vpsAccess") or "").strip()
        missing = []
        if not self.api_url:
            if not dynamic_host:
                missing.append("vpsHost")
            if not dynamic_access:
                missing.append("vpsAccess")
        if missing:
            return {
                "local_saved": False, "remote_configured": bool(dynamic_host),
                "remote_uploaded": False, "missing_fields": missing,
                "remote_error": "Manual upload needs " + " and ".join(missing),
            }

        api_url = self.api_url or f"{dynamic_host}/dual/offset"
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        central_payload = {
            **self._remote_payload(payload), "offset": self._remote_offset_result(result),
        }
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await logged_http_request(
                    self.activity, client, "POST", f"{api_url}/report", "offset-db",
                    json=central_payload, headers=headers,
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return {
                "local_saved": False, "remote_configured": True,
                "remote_uploaded": False, "remote_status": exc.response.status_code,
                "remote_error": f"Remote report returned HTTP {exc.response.status_code}: {exc.response.text[:240]}",
            }
        except Exception as exc:
            return {
                "local_saved": False, "remote_configured": True,
                "remote_uploaded": False,
                "remote_error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }

        required = ("media_key", "resolution", "video_fingerprint", "audio_fingerprint")
        local_saved = all(payload.get(name) not in (None, "") for name in required)
        local_error = None
        if local_saved:
            try:
                await asyncio.to_thread(self._local_put, payload, result)
            except (OSError, sqlite3.Error, ValueError) as exc:
                local_saved = False
                local_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        status = {
            "local_saved": local_saved, "remote_configured": True,
            "remote_uploaded": True, "remote_status": response.status_code,
        }
        if local_error:
            status["local_error"] = local_error
        return status
