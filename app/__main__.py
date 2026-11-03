"""Entry point: `python -P -m app`. The only place that reads .env.

uvicorn runs without an access log (it would log paths and query strings), without
trusting proxy headers, without a server header, and with the redacting log config.
"""

from __future__ import annotations

import sys

import uvicorn
from opentelemetry.sdk.trace import TracerProvider

from app.config.settings import Settings
from app.main import create_app
from app.observability.logs import LOG_CONFIG
from app.observability.tracing import PlaintextRefused

# Exit code for a configuration the entry point refuses to start with.
REFUSED = 2


def server_config(
    settings: Settings, tracer_provider: TracerProvider | None = None
) -> uvicorn.Config:
    app = create_app(settings, tracer_provider=tracer_provider, bind_host=settings.host)
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_config=LOG_CONFIG,
        http="h11",
        ws="none",
    )


class StartRefused(ValueError):
    """A configuration the entry point refuses to serve. The message names no value."""


def listening_sockets(server: uvicorn.Server) -> list:
    """Scaffolding (PR 7, red): the sockets a started server listens on."""
    return []


def verify_listeners(sockets: list, port: int) -> None:
    """Scaffolding (PR 7, red): checks nothing yet."""
    return


def main() -> int:
    settings = Settings(_env_file=".env")
    try:
        config = server_config(settings)
    except PlaintextRefused as refused:
        print(f"refused to start: {refused}", file=sys.stderr)
        return REFUSED
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
