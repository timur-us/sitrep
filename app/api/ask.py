"""POST /api/ask (M1 plan, PR 12). Scaffolding: the names the tests import, no route yet."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI

from app.config.settings import Settings
from app.embeddings.base import Embedder
from app.embeddings.onnx import OnnxEmbedder
from app.harness import loop
from app.harness.handlers import read_handlers
from app.harness.model import ModelClient
from app.harness.model_client import build_model_client

__all__ = ["build_model_client", "read_handlers"]

ROUTE = "/api/ask"
MAX_BODY = 16_384
MAX_QUESTION = loop.MAX_QUESTION
MAX_MANDATE_ID = loop.MAX_MANDATE_ID
REQUESTS_PER_WINDOW = 10
WINDOW_SECONDS = 60.0
DEADLINE_SECONDS = 90.0

INVALID = "invalid request"
TOO_MANY = "too many requests"
NO_MODEL = "model unavailable"
NO_SEARCH = "search unavailable"
NO_DATABASE = "database unavailable"
MODEL_FAILED = "the model call failed"
FAILED = "the request failed"

STATUS: Mapping[str, tuple[int, str]] = {}


def build_embedder() -> Embedder:
    return OnnxEmbedder()


class RateLimit:
    def __init__(self, per_window: int, window_seconds: float, clock: Callable[[], float]) -> None:
        pass

    def admit(self, advisor_id: str) -> bool:
        raise NotImplementedError("M1 PR 12")

    def release(self, advisor_id: str) -> None:
        raise NotImplementedError("M1 PR 12")


class DeadlineClient:
    def __init__(self, inner: ModelClient, ends_at: float, clock: Callable[[], float]) -> None:
        self.model = getattr(inner, "model", "")

    def create(self, **request: Any) -> Mapping[str, Any]:
        raise NotImplementedError("M1 PR 12")


def mount(
    app: FastAPI,
    settings: Settings,
    *,
    model_client: ModelClient | None = None,
    embedder: Embedder | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Scaffolding: mounts nothing yet."""
