"""The FastAPI app. Scaffolding: the routes and middleware arrive in the next commit."""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from app.config.settings import Settings


def create_app(
    settings: Settings,
    tracer_provider: TracerProvider | None = None,
    bind_host: str | None = None,
) -> FastAPI:
    return FastAPI()
