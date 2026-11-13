"""The dev login shim (ADR 0009): a local login for the synthetic advisors.

It exists only where app/__main__.py binds exactly 127.0.0.1 with the dev login on (the
DEV_AUTH setting, read with the product prefix, exactly "1"); create_app() mounts it
under no other condition, and .dockerignore keeps this file out of the image, where the
entry point refuses the dev login because it cannot import it.

What it adds, all in this file:
- DevShimMiddleware, around every route, the shim's own and the app's:
  - The TCP peer must be 127.0.0.1, otherwise 403, whatever bind the app was built for.
  - Host must be exactly 127.0.0.1:<port>, one header, otherwise 400. A name that
    resolves to 127.0.0.1 (DNS rebinding) or another port is refused.
  - Every method but GET needs one Origin header of exactly http://127.0.0.1:<port>,
    otherwise 403. Missing, `null`, `localhost`, another port, scheme or host are
    refused. A Sec-Fetch-Site header, when sent, must be exactly same-origin.
  - The session cookie, when it is signed by this process and names a live session,
    attaches that session's advisor to the request (identity.attach()). Nothing else
    the request carries is read for identity.
  - Any other ASGI connection (a websocket) is closed: the app has none.
- GET /dev/login: a page that lists the advisors from branding, one button each.
- POST /dev/login: Content-Type exactly application/x-www-form-urlencoded and a body of
  exactly `advisor=<id>`, at most MAX_BODY bytes (400 otherwise; 403 for a well-formed
  id that names no advisor). A random session id is kept in process memory, mapped to
  the advisor, and sent as `<id>.<HMAC-SHA256>` under a key drawn when the app is built.
  The cookie is HttpOnly, SameSite=Strict, Path=/, without Domain, Max-Age or Expires.
  A session the request already carried is ended. A request with two session cookies
  carries none: it is not signed in, and its login or logout ends neither session.
  Sessions do not expire by time, and the store has no size limit (ADR 0009, Watch).
- POST /dev/logout: ends the session and clears the cookie.

The page after a login links to the advisor page at / (its ask box), to /api/mandates and
back to the login, all relative, on the same origin.

No redirects: the login answers with a page. The app sends Referrer-Policy:
no-referrer, under which a browser sends `Origin: null` even on a same-origin POST; the
login page sets its own referrer policy to same-origin with a meta element, so the form
sends its real origin (tests/web/test_dev_login.py).

Nothing here logs: no advisor id, session id or cookie reaches a log or a span.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.identity import attach, issue
from app.config import branding
from app.config.settings import LOOPBACK
from app.http import host_check

COOKIE = f"{branding.SLUG}_dev_session"
FORM_TYPE = "application/x-www-form-urlencoded"
# `advisor=<id>` is all a login body holds.
MAX_BODY = 256
LOGIN_BODY = re.compile(rb"advisor=([a-z]{1,32})")
ADVISORS = {person.id: person for person in branding.ADVISORS}


class Sessions:
    """Server-side sessions in process memory, and the cookies that name them."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._advisors: dict[str, str] = {}

    def _signature(self, session_id: str) -> str:
        return hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()

    def _session_id(self, cookie: str) -> str | None:
        session_id, dot, signature = cookie.rpartition(".")
        if not dot or not session_id:
            return None
        expected = self._signature(session_id)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            return None
        return session_id

    def start(self, advisor_id: str) -> str:
        """A new session for advisor_id; returns its cookie value."""
        session_id = secrets.token_urlsafe(32)
        self._advisors[session_id] = advisor_id
        return f"{session_id}.{self._signature(session_id)}"

    def advisor(self, cookie: str | None) -> str | None:
        session_id = self._session_id(cookie) if cookie else None
        return self._advisors.get(session_id) if session_id else None

    def end(self, cookie: str | None) -> None:
        session_id = self._session_id(cookie) if cookie else None
        if session_id:
            self._advisors.pop(session_id, None)


def session_cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The value of the one session cookie the request carries, or None (none, or more
    than one: a cookie set for 127.0.0.1 by another local port would be a second)."""
    values = []
    for name, value in headers:
        if name != b"cookie":
            continue
        for pair in value.decode("latin-1").split(";"):
            key, _, cookie = pair.strip().partition("=")
            if key == COOKIE:
                values.append(cookie)
    return values[0] if len(values) == 1 else None


def _values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key == name]


class DevShimMiddleware:
    """Host and Origin checks, and the session's advisor onto the request."""

    def __init__(self, app: ASGIApp, sessions: Sessions, port: int) -> None:
        self.app = app
        self.sessions = sessions
        self.hosts = frozenset({f"{LOOPBACK}:{port}".encode()})
        self.origin = f"http://{LOOPBACK}:{port}".encode()

    def refusal(self, scope: Scope) -> Response | None:
        # The TCP peer (review finding sec-2): uvicorn runs with proxy_headers off, so
        # scope["client"] is the connection's own address, which a remote host cannot
        # choose. This holds whatever bind_host create_app() was given. A Unix socket
        # has no peer address (None) and is refused too.
        client = scope.get("client")
        if not client or client[0] != LOOPBACK:
            return PlainTextResponse("Forbidden", status_code=403)
        headers = scope["headers"]
        # The app's own Host check (app/http/host_check.py) ran before this; the shim
        # repeats it with loopback alone, whatever the allowed_hosts setting says.
        if not host_check.host_allowed(headers, self.hosts):
            return PlainTextResponse("Bad Request", status_code=400)
        if scope["method"] != "GET":
            if _values(headers, b"origin") != [self.origin]:
                return PlainTextResponse("Forbidden", status_code=403)
            site = _values(headers, b"sec-fetch-site")
            if site and site != [b"same-origin"]:
                return PlainTextResponse("Forbidden", status_code=403)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        refused = self.refusal(scope)
        if refused is not None:
            await refused(scope, receive, send)
            return
        advisor_id = self.sessions.advisor(session_cookie(scope["headers"]))
        if advisor_id is not None:
            attach(scope, issue(advisor_id))
        await self.app(scope, receive, send)


def _page(body: str) -> str:
    title = html.escape(f"{branding.PRODUCT.name} dev login")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        # Overrides the app's no-referrer for this page, so the form's POST carries its
        # real Origin instead of "null" (Fetch, "serializing a request origin").
        '<meta name="referrer" content="same-origin">\n'
        f"<title>{title}</title>\n</head>\n<body>\n{body}</body>\n</html>\n"
    )


def login_page() -> str:
    buttons = "".join(
        f'<button type="submit" name="advisor" value="{html.escape(person.id)}">'
        f"{html.escape(person.name)}</button>\n"
        for person in branding.ADVISORS
    )
    return _page(
        "<h1>Dev login</h1>\n"
        "<p>Local development only, synthetic advisors. Not in the image.</p>\n"
        f'<form method="post" action="/dev/login">\n{buttons}</form>\n'
        '<form method="post" action="/dev/logout">\n'
        '<button type="submit">Sign out</button>\n</form>\n'
    )


def signed_in_page(name: str) -> str:
    return _page(
        f"<p>Signed in as {html.escape(name)}.</p>\n"
        '<p><a href="/">Ask a question</a> · <a href="/api/mandates">Your mandates</a>'
        ' · <a href="/dev/login">Dev login</a></p>\n'
    )


async def _body(request: Request) -> bytes | None:
    """The request body, or None once it grows past MAX_BODY."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return None
    return body


def mount(app: FastAPI, port: int) -> None:
    """Adds the shim's routes and middleware to app, for a server on 127.0.0.1:port.

    Called by create_app() before the security headers middleware, so that the shim's
    400 and 403 carry those headers too.
    """
    sessions = Sessions()
    page = login_page()

    async def dev_login_page() -> HTMLResponse:
        return HTMLResponse(page)

    async def dev_login(request: Request) -> Response:
        if request.headers.get("content-type") != FORM_TYPE:
            return PlainTextResponse("Bad Request", status_code=400)
        body = await _body(request)
        match = LOGIN_BODY.fullmatch(body) if body is not None else None
        if match is None:
            return PlainTextResponse("Bad Request", status_code=400)
        person = ADVISORS.get(match.group(1).decode())
        if person is None:
            return PlainTextResponse("Forbidden", status_code=403)
        sessions.end(session_cookie(request.scope["headers"]))
        response = HTMLResponse(signed_in_page(person.name))
        response.set_cookie(
            COOKIE, sessions.start(person.id), path="/", httponly=True, samesite="strict"
        )
        return response

    async def dev_logout(request: Request) -> Response:
        sessions.end(session_cookie(request.scope["headers"]))
        response = HTMLResponse(_page("<p>Signed out.</p>\n"))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    # On the app itself, not an included router, so the route list shows them.
    app.add_api_route("/dev/login", dev_login_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/dev/login", dev_login, methods=["POST"])
    app.add_api_route("/dev/logout", dev_logout, methods=["POST"])
    app.add_middleware(DevShimMiddleware, sessions=sessions, port=port)
