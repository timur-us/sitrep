"""The FastAPI app: create_app() builds it, app/__main__.py serves it.

No redirects anywhere (threat model, EchoLeak lesson): trailing slashes are not
redirected, and no route fetches or forwards to a URL. No docs or OpenAPI routes. The
global tracer provider is never set.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from opentelemetry.sdk.trace import TracerProvider

from app.api import mandates
from app.config import branding
from app.config.settings import LOOPBACK, Settings
from app.db import session
from app.http.host_check import HostCheckMiddleware, allowed_hosts
from app.http.security_headers import SecurityHeadersMiddleware, response_headers
from app.observability import tracing
from app.observability.logs import asyncio_exception_handler, tag_trace_id

INDEX = Path(__file__).parent / "web" / "static" / "index.html"
TITLE = "{{title}}"


def index_html() -> str:
    """The placeholder page, its title filled in from branding."""
    title = html.escape(f"{branding.PRODUCT.name} — {branding.BANK.display_name}")
    return INDEX.read_text(encoding="utf-8").replace(TITLE, title)


async def internal_error(request: Request, exc: Exception) -> Response:
    # Starlette sends this 500 from outside the middleware stack, so the headers are
    # set here. The trace id goes onto the exception for uvicorn's log line.
    tag_trace_id(exc)
    return PlainTextResponse(
        "Internal Server Error", status_code=500, headers=dict(response_headers(request.url.path))
    )


def create_app(
    settings: Settings,
    tracer_provider: TracerProvider | None = None,
    bind_host: str | None = None,
) -> FastAPI:
    """bind_host is the address the entry point binds; None when it is not known.

    With trace_plaintext at "1" (the prefixed variable, ADR 0009) the default console
    chain is required, on 127.0.0.1.
    """
    if tracer_provider is None:
        tracer_provider = tracing.build_tracer_provider(settings, bind_host)
    elif settings.trace_plaintext_on:
        raise tracing.PlaintextRefused(
            f"{tracing.PLAINTEXT}=1 is allowed with the default console exporter only"
        )

    # Lazy (app/db/session.py): opening the pool connects nobody, so the app starts and
    # answers /health with the database down. /api/mandates uses it.
    pool = session.make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # An exception in a task nobody awaits logs its type and the trace id only, on the
        # loop that serves the app (ADR 0007, M1 PR 5).
        asyncio.get_running_loop().set_exception_handler(asyncio_exception_handler)
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(
        redirect_slashes=False, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.pool = pool
    page = index_html()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(page)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Always mounted: without a login (the shim off, Entra ID not before M3) it answers 401.
    mandates.mount(app)

    # The dev login (ADR 0009): only for exactly "1", and only when the entry point binds
    # exactly 127.0.0.1 (it has refused every other host by then). Imported here, not at
    # the top: the image does not carry the module. Mounted before the security headers,
    # so its refusals carry them.
    if bind_host == LOOPBACK and settings.dev_auth_on:
        from app.auth import dev_shim

        dev_shim.mount(app, port=settings.port)

    # Every route, shim or not (M1 PR 5, ADR 0009): added after the shim, so it runs
    # first; before the security headers, so its 400 carries them.
    app.add_middleware(HostCheckMiddleware, allowed=allowed_hosts(settings))
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(Exception, internal_error)
    tracing.instrument(app, tracer_provider)
    return app
