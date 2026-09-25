"""Request admission for every Engine route (threat model B3): per-launch
bearer token, webview-only Origin allowlist, HTTP-only protocol."""

import os
import secrets
import sys

from starlette.types import ASGIApp, Receive, Scope, Send

from readily_engine.server.wire import ErrorCode, error_bytes

WEBVIEW_ORIGIN = b"tauri://localhost"
DEV_ORIGIN = b"http://127.0.0.1:1420"
DEV_ORIGIN_VAR = "READILY_ENGINE_ALLOW_DEV_ORIGIN"


def allowed_origins() -> frozenset[bytes]:
    """The browser origins this launch will answer. Read once, when the app
    is built, so nothing can widen it later."""
    if os.environ.get(DEV_ORIGIN_VAR) == "1":
        return frozenset({WEBVIEW_ORIGIN, DEV_ORIGIN})
    return frozenset({WEBVIEW_ORIGIN})


def new_token() -> str:
    """A fresh 128-bit per-launch bearer token, hex-encoded."""
    return secrets.token_hex(16)


def launch_token() -> str:
    """The per-launch token, from the supervisor via environment — never
    argv (`ps` shows argv to every local user; threat model B3). Standalone
    runs get a fresh generated token, announced on stderr."""
    token = os.environ.get("READILY_ENGINE_TOKEN")
    if token:
        return token
    token = new_token()
    print(f"READILY_ENGINE_TOKEN not set; using {token}", file=sys.stderr)
    return token


def _header(scope: Scope, name: bytes) -> bytes | None:
    # First occurrence, matching Starlette's Headers.get — a duplicate
    # header must not read differently here than in a route handler.
    return next((v for k, v in scope["headers"] if k == name), None)


class BearerTokenMiddleware:
    """Rejects any request that lacks the launch token.

    Pure ASGI (not BaseHTTPMiddleware) so SSE and other streaming responses
    pass through untouched. Runs before routing: an unauthenticated probe
    can't enumerate routes. WebSocket handshakes are refused outright —
    "every route requires the token" must stay true when a ws route lands,
    and this middleware can't guard a protocol it passes through.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close"})
            return
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        supplied = _header(scope, b"authorization") or b""
        if not secrets.compare_digest(supplied, self._expected):
            body = error_bytes(ErrorCode.UNAUTHORIZED)
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


class OriginAllowlistMiddleware:
    """403s any request bearing a browser Origin outside the webview's.

    Requests without an Origin header (the supervisor, curl) pass — the
    bearer token is their gate. A browser can't omit or forge Origin, so
    this shuts out malicious web pages even if the token ever leaked
    (threat model B3).
    """

    def __init__(self, app: ASGIApp, allowed: frozenset[bytes]) -> None:
        self._app = app
        self._allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            origin = _header(scope, b"origin")
            if origin is not None and origin not in self._allowed:
                body = error_bytes(ErrorCode.FORBIDDEN_ORIGIN)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)
