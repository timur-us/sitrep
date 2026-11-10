"""The Host check: an exact allowlist on every route, with or without the dev login.

M1 PR 5, Timur's answer 4 to the PR 7 questions ("Host check without the shim in M1"),
ADR 0009. A page on any origin can make a browser send requests to the app, and a name
that resolves to 127.0.0.1 (DNS rebinding) lets that page read the answers. So a request
is served only when it carries exactly one Host header, byte for byte one of the allowed
values; anything else is a plain 400, carrying the app's security headers (the
middleware sits inside SecurityHeadersMiddleware), never a redirect.

The allowed values are the allowed_hosts setting, or exactly 127.0.0.1:<port> when it is
empty. The dev login's own check (app/auth/dev_shim.py) calls host_allowed() with
127.0.0.1:<port> alone, whatever the setting says.

Websocket scopes are closed: the app has none, and uvicorn runs with ws="none".
"""

from __future__ import annotations

from fastapi.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config.settings import Settings


def allowed_hosts(settings: Settings) -> frozenset[bytes]:
    """The Host values the app answers, as the header carries them."""
    return frozenset(value.encode("ascii") for value in settings.allowed_host_values)


def host_allowed(headers: list[tuple[bytes, bytes]], allowed: frozenset[bytes]) -> bool:
    """Exactly one Host header, and its value one of allowed."""
    values = [value for name, value in headers if name == b"host"]
    return len(values) == 1 and values[0] in allowed


class HostCheckMiddleware:
    def __init__(self, app: ASGIApp, allowed: frozenset[bytes]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if not host_allowed(scope["headers"], self.allowed):
            await PlainTextResponse("Bad Request", status_code=400)(scope, receive, send)
            return
        await self.app(scope, receive, send)
