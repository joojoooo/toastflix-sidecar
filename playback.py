import hashlib
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_NAMES = {
    "access",
    "authorization",
    "cookie",
    "headers",
    "key",
    "playlist",
    "token",
    "vpsaccess",
}


def _safe_url(value: str) -> str:
    """Keep a URL useful for debugging without exposing signed query strings."""
    try:
        parts = urlsplit(str(value))
    except ValueError:
        return "[redacted URL]"
    if not parts.scheme or not parts.netloc:
        return str(value)[:300]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def safe_metadata(value, key: str = ""):
    """Recursively redact request credentials before retaining debug metadata."""
    normalized = key.replace("_", "").lower()
    if normalized in SENSITIVE_NAMES or any(
        marker in normalized for marker in ("secret", "password", "credential")
    ):
        if normalized == "headers" and isinstance(value, dict):
            return {"names": sorted(str(name) for name in value)}
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): safe_metadata(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [safe_metadata(item) for item in value[:100]]
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return _safe_url(value)
    if isinstance(value, str):
        return value[:1000]
    return value


def audio_debug_metadata(metadata: dict) -> dict:
    segments = metadata.get("segs") or []
    durations = metadata.get("durs") or []
    result = {
        str(key): safe_metadata(value, str(key))
        for key, value in metadata.items()
        if key not in {"segs", "headers"}
    }
    result["segments"] = {
        "count": len(segments),
        "duration_seconds": round(sum(float(value) for value in durations), 3),
        "first_url": _safe_url(segments[0]) if segments else None,
        "last_url": _safe_url(segments[-1]) if segments else None,
    }
    result["header_names"] = sorted(str(name) for name in (metadata.get("headers") or {}))
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

    def register(self, hid: str, token: str, metadata: dict, cached_audio: bool = False) -> str:
        now = time.time()
        playback_id = self._playback_id(hid, token)
        token_hash = self._token_hash(token)
        player = self._players.get(playback_id)
        if player is None:
            player = {
                "playback_id": playback_id,
                "hid": hid,
                "token_hash": token_hash,
                "created_at": now,
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
            }
            self._players[playback_id] = player
            self._keys[(token_hash, hid)] = playback_id
        player.update({
            "updated_at": now,
            "cached_audio": bool(cached_audio),
            "audio_metadata": audio_debug_metadata(metadata),
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
            "cache_key": cache_key,
            "resolution": payload.get("resolution"),
            "cache_hit": cache_hit,
            "cache_source": cache_source,
            "sync_result": safe_metadata(result),
            "sync_metadata": safe_metadata(payload),
        })
        if result.get("status", "ok") == "ok" and isinstance(result.get("offset"), (int, float)):
            player["current_offset"] = float(result["offset"])
            player["current_rate"] = float(result.get("rate", 1.0))
            player["control_source"] = (
                f"cached:{cache_source or 'unknown'}" if result.get("cached") else "measured"
            )
            player["revision"] += 1
        event_kind = (
            "offset-cache-hit" if result.get("cached")
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

    @staticmethod
    def _public_player(player: dict) -> dict:
        result = {key: value for key, value in player.items() if key != "token_hash"}
        result["pending_player_request"] = player["revision"] > player["applied_revision"]
        last_request = player.get("last_request_at") or 0
        result["active"] = time.time() - last_request < 120 if last_request else False
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
