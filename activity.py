import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from pathlib import Path


TEXT_CONTENT_TYPES = (
    "application/json",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/xml",
    "application/x-www-form-urlencoded",
    "text/",
)


def _headers_dict(headers) -> dict:
    if headers is None:
        return {}
    items = headers.multi_items() if hasattr(headers, "multi_items") else headers
    result = {}
    for name, value in items:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        name, value = str(name), str(value)
        if name in result:
            result[name] = [result[name], value] if not isinstance(result[name], list) else [*result[name], value]
        else:
            result[name] = value
    return result


def body_record(body: bytes, headers: dict, include_binary: bool = False):
    if not body:
        return None
    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()
    textual = any(marker in content_type for marker in TEXT_CONTENT_TYPES)
    if textual:
        return {
            "encoding": "utf-8",
            "content": body.decode("utf-8", errors="replace"),
            "byte_length": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    result = {
        "encoding": "base64" if include_binary else "binary",
        "byte_length": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if include_binary:
        result["content"] = base64.b64encode(body).decode("ascii")
    return result


class ActivityLog:
    """Persistent, inspectable sidecar traffic journal with live subscribers."""

    def __init__(self, path: str, max_rows: int = 5000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max(500, int(max_rows))
        self._subscribers: set[asyncio.Queue] = set()
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_db(self):
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    direction TEXT NOT NULL,
                    category TEXT NOT NULL,
                    method TEXT,
                    url TEXT,
                    status INTEGER,
                    duration_ms REAL,
                    request_bytes INTEGER NOT NULL,
                    response_bytes INTEGER NOT NULL,
                    detail TEXT NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS activity_created_at ON activity(created_at DESC)"
            )

    def _insert(self, detail: dict) -> dict:
        request_body = detail.get("request_body") or {}
        response_body = detail.get("response_body") or {}
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO activity
                (created_at, direction, category, method, url, status, duration_ms,
                 request_bytes, response_bytes, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    detail["created_at"], detail["direction"], detail["category"],
                    detail.get("method"), detail.get("url"), detail.get("status"),
                    detail.get("duration_ms"), int(request_body.get("byte_length") or 0),
                    int(response_body.get("byte_length") or 0),
                    json.dumps(detail, ensure_ascii=False, separators=(",", ":"), default=str),
                ),
            )
            activity_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM activity WHERE id <= (SELECT MAX(id) - ? FROM activity)",
                (self.max_rows,),
            )
        return self._summary(activity_id, detail)

    @staticmethod
    def _summary(activity_id: int, detail: dict) -> dict:
        return {
            "id": activity_id,
            "created_at": detail["created_at"],
            "direction": detail["direction"],
            "category": detail["category"],
            "method": detail.get("method"),
            "url": detail.get("url"),
            "status": detail.get("status"),
            "duration_ms": detail.get("duration_ms"),
            "request_bytes": int((detail.get("request_body") or {}).get("byte_length") or 0),
            "response_bytes": int((detail.get("response_body") or {}).get("byte_length") or 0),
            "error": detail.get("error"),
            "metadata": detail.get("metadata") or {},
        }

    async def record(self, **detail) -> dict:
        detail.setdefault("created_at", time.time())
        summary = await asyncio.to_thread(self._insert, detail)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(summary)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(summary)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return summary

    def _recent(self, limit: int):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, created_at, direction, category, method, url, status,
                          duration_ms, request_bytes, response_bytes, detail
                   FROM activity ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        summaries = []
        for row in rows:
            detail = json.loads(row[10])
            summaries.append({
                "id": row[0], "created_at": row[1], "direction": row[2],
                "category": row[3], "method": row[4], "url": row[5],
                "status": row[6], "duration_ms": row[7], "request_bytes": row[8],
                "response_bytes": row[9], "error": detail.get("error"),
                "metadata": detail.get("metadata") or {},
            })
        return summaries

    async def recent(self, limit: int = 300):
        return await asyncio.to_thread(self._recent, limit)

    def _get(self, activity_id: int):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT detail FROM activity WHERE id = ?", (int(activity_id),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    async def get(self, activity_id: int):
        return await asyncio.to_thread(self._get, activity_id)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._subscribers.discard(queue)


def inbound_category(path: str) -> str:
    if path.startswith("/dual/aud/"):
        return "stremio-media"
    if path.startswith(("/dual/", "/sync", "/offset/", "/session")):
        return "stremio-api"
    if path.startswith(("/dashboard", "/api/dashboard")):
        return "dashboard"
    return "server"


class TrafficLogMiddleware:
    """ASGI middleware that retains complete requests and observes streamed responses."""

    def __init__(self, app, activity: ActivityLog):
        self.app = app
        self.activity = activity

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_chunks = []
        response_size = 0
        response_hash = hashlib.sha256()
        response_chunks = []
        response_status = None
        response_headers = {}
        request_headers = _headers_dict(scope.get("headers") or [])
        path = str(scope.get("path") or "")

        request_messages = []
        while True:
            message = await receive()
            request_messages.append(message)
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break
        request_chunks.extend(
            message.get("body") or b""
            for message in request_messages
            if message.get("type") == "http.request"
        )
        replay_index = 0

        async def observed_receive():
            nonlocal replay_index
            if replay_index < len(request_messages):
                message = request_messages[replay_index]
                replay_index += 1
                return message
            message = await receive()
            return message

        async def observed_send(message):
            nonlocal response_status, response_headers, response_size
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status") or 0)
                response_headers = _headers_dict(message.get("headers") or [])
            elif message.get("type") == "http.response.body":
                chunk = message.get("body") or b""
                response_size += len(chunk)
                response_hash.update(chunk)
                content_type = str(response_headers.get("content-type") or "").lower()
                if "text/event-stream" not in content_type and any(
                    marker in content_type for marker in TEXT_CONTENT_TYPES
                ):
                    response_chunks.append(chunk)
            await send(message)

        error = None
        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            query = (scope.get("query_string") or b"").decode("latin-1")
            scheme = scope.get("scheme") or "http"
            host = request_headers.get("host", "")
            url = f"{scheme}://{host}{path}" + (f"?{query}" if query else "")
            raw_response = b"".join(response_chunks)
            response_body = body_record(raw_response, response_headers)
            if response_size and response_body is None:
                response_body = {
                    "encoding": "binary",
                    "byte_length": response_size,
                    "sha256": response_hash.hexdigest(),
                }
            client = scope.get("client") or (None, None)
            try:
                await self.activity.record(
                    direction="inbound",
                    category=inbound_category(path),
                    method=scope.get("method"),
                    url=url,
                    status=response_status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    request_headers=request_headers,
                    request_body=body_record(b"".join(request_chunks), request_headers, include_binary=True),
                    response_headers=response_headers,
                    response_body=response_body,
                    error=error,
                    metadata={
                        "path": path,
                        "route": (scope.get("route") and getattr(scope["route"], "path", None)),
                        "client_host": client[0],
                        "client_port": client[1],
                        "http_version": scope.get("http_version"),
                    },
                )
            except Exception as log_error:
                print(f"[sidecar activity] inbound log failed: {type(log_error).__name__}")


async def logged_http_request(activity: ActivityLog | None, client, method: str,
                              url: str, category: str, **kwargs):
    started = time.perf_counter()
    response = None
    failed_request = None
    error = None
    try:
        response = await client.request(method, url, **kwargs)
        return response
    except BaseException as exc:
        failed_request = getattr(exc, "request", None)
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if activity is not None:
            request = response.request if response is not None else failed_request
            supplied_headers = kwargs.get("headers") or {}
            request_headers = _headers_dict(request.headers if request is not None else supplied_headers.items())
            request_content = b""
            if request is not None:
                try:
                    request_content = request.content
                except Exception:
                    request_content = b""
            elif "json" in kwargs:
                request_content = json.dumps(kwargs["json"], ensure_ascii=False, default=str).encode()
                request_headers.setdefault("content-type", "application/json")
            response_headers = _headers_dict(response.headers) if response is not None else {}
            response_content = response.content if response is not None else b""
            try:
                await activity.record(
                    direction="outbound",
                    category=category,
                    method=method.upper(),
                    url=str(url),
                    status=response.status_code if response is not None else None,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    request_headers=request_headers,
                    request_body=body_record(request_content, request_headers, include_binary=True),
                    response_headers=response_headers,
                    response_body=body_record(response_content, response_headers),
                    error=error,
                    metadata={},
                )
            except Exception as log_error:
                print(f"[sidecar activity] outbound log failed: {type(log_error).__name__}")
