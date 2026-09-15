import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx


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

    def __init__(self, path: str, api_url: str = "", api_token: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token.strip()
        self._init_db()

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
        result = {name: payload[name] for name in cls.REMOTE_FIELDS if name in payload}
        if payload.get("vpsAccess"):
            result["access"] = payload["vpsAccess"]
        return result

    async def lookup(self, payload: dict):
        local_result = await asyncio.to_thread(self._local_get, payload["cache_key"])
        if local_result and (local_result.get("details") or {}).get("custom"):
            local_result["_cache_source"] = "local-custom"
            return local_result
        api_url = self.api_url
        if not api_url and payload.get("vpsHost"):
            api_url = f"{str(payload['vpsHost']).rstrip('/')}/dual/offset"
        if api_url:
            try:
                headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
                central_payload = self._remote_payload(payload)
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.post(f"{api_url}/lookup", json=central_payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("found") and data.get("offset") is not None:
                        result = data["offset"]
                        if not isinstance(result, dict):
                            result = {"status": "ok", "offset": float(result), "rate": 1.0}
                        cache_value = dict(result.get("details") or result)
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

    async def report(self, payload: dict, result: dict):
        await asyncio.to_thread(self._local_put, payload, result)
        api_url = self.api_url
        if not api_url and payload.get("vpsHost"):
            api_url = f"{str(payload['vpsHost']).rstrip('/')}/dual/offset"
        if not api_url:
            return {"local_saved": True, "remote_configured": False, "remote_uploaded": False}
        try:
            headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
            central_payload = {**self._remote_payload(payload), "offset": result}
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    f"{api_url}/report",
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
        details = dict((record or {}).get("details") or {})
        details.update({
            "status": "ok",
            "offset": float(offset),
            "rate": float(rate),
            "confidence": float(details.get("confidence", record.get("confidence") or 0.0)),
            "custom": True,
            "custom_note": str(note or "")[:500],
            "custom_updated_at": time.time(),
        })
        source = record or metadata or {}
        required = ("media_key", "resolution", "video_fingerprint", "audio_fingerprint")
        if any(name not in source for name in required):
            raise KeyError("playback does not have enough metadata to persist this offset")
        payload = {"cache_key": cache_key, **{name: source[name] for name in required}}
        await asyncio.to_thread(self._local_put, payload, details)
        return await self.get(cache_key)

    async def upload(self, cache_key: str):
        """Upload one locally edited record using the configured central API."""
        record = await self.get(cache_key)
        if not record:
            raise KeyError("offset record not found")
        if not self.api_url:
            return {
                "local_saved": True,
                "remote_configured": False,
                "remote_uploaded": False,
                "remote_error": "OFFSET_API_URL is not configured",
            }
        payload = {
            "cache_key": record["cache_key"],
            "media_key": record["media_key"],
            "resolution": record["resolution"],
            "video_fingerprint": record["video_fingerprint"],
            "audio_fingerprint": record["audio_fingerprint"],
        }
        return await self.report(payload, dict(record.get("details") or {}))
