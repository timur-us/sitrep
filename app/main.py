"""The FastAPI app: create_app() builds it, app/__main__.py serves it.

No redirects anywhere (threat model, EchoLeak lesson): trailing slashes are not
redirected, and no route fetches or forwards to a URL. No docs or OpenAPI routes. The
global tracer provider is never set.
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from opentelemetry.sdk.trace import TracerProvider

from app.config import branding
from app.config.settings import Settings
from app.http.security_headers import SecurityHeadersMiddleware, response_headers
from app.observability import tracing
from app.observability.logs import tag_trace_id

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

    With TRACE_PLAINTEXT=1 the default console chain is required, on 127.0.0.1.
    """
    if tracer_provider is None:
        tracer_provider = tracing.build_tracer_provider(settings, bind_host)
    elif settings.trace_plaintext_on:
        raise tracing.PlaintextRefused(
            "TRACE_PLAINTEXT=1 is allowed with the default console exporter only"
        )

    app = FastAPI(redirect_slashes=False, docs_url=None, redoc_url=None, openapi_url=None)
    page = index_html()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(page)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(Exception, internal_error)
    tracing.instrument(app, tracer_provider)
    return app
