"""The Host check (M1 PR 5): scaffolding for the tests, inert until the next commit."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config.settings import LOOPBACK, Settings


def allowed_hosts(settings: Settings) -> frozenset[bytes]:
    return frozenset({f"{LOOPBACK}:{settings.port}".encode()})


def host_allowed(headers: list[tuple[bytes, bytes]], allowed: frozenset[bytes]) -> bool:
    return True


class HostCheckMiddleware:
    def __init__(self, app: ASGIApp, allowed: frozenset[bytes]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)
