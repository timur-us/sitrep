"""Headers on every response: a strict CSP, no MIME sniffing, no referrer, same-origin
resource and opener policies, no caching of API responses, and the trace id.

The CSP allows the page's own origin only and no inline code: `img-src 'self'` is one
of the EchoLeak hardening layers (threat model). Headers the app sets here replace any
of the same name a route sets.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.tracing import current_trace_id

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
HEADERS = (
    ("content-security-policy", CSP),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-resource-policy", "same-origin"),
    ("cross-origin-opener-policy", "same-origin"),
)
NO_STORE = ("cache-control", "no-store")
TRACE_ID = "x-trace-id"
OWNED = frozenset({*(name for name, _ in HEADERS), NO_STORE[0], TRACE_ID})


def is_page(path: str) -> bool:
    """The static UI, which may be cached. Every other path is an API response."""
    return path == "/" or path.startswith("/static/")


def response_headers(path: str) -> list[tuple[str, str]]:
    headers = list(HEADERS)
    if not is_page(path):
        headers.append(NO_STORE)
    trace_id = current_trace_id()
    if trace_id is not None:
        headers.append((TRACE_ID, trace_id))
    return headers


class SecurityHeadersMiddleware:
    """Pure ASGI, so it runs inside the request's span and sees its trace id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.decode("latin-1").lower() not in OWNED
                ]
                ours = [(name.encode(), value.encode()) for name, value in response_headers(path)]
                message = {**message, "headers": kept + ours}
            await send(message)

        await self.app(scope, receive, send_with_headers)
