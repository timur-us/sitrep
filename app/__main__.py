"""Entry point, `python -P -m app`. Scaffolding: uvicorn's defaults until the next commit."""

from __future__ import annotations

import sys

import uvicorn
from opentelemetry.sdk.trace import TracerProvider

from app.config.settings import Settings
from app.main import create_app


def server_config(
    settings: Settings, tracer_provider: TracerProvider | None = None
) -> uvicorn.Config:
    app = create_app(settings, tracer_provider=tracer_provider, bind_host=settings.host)
    return uvicorn.Config(app, host=settings.host, port=settings.port)


def main() -> int:
    uvicorn.Server(server_config(Settings())).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
