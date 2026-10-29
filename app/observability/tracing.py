"""Tracing. Scaffolding: the allowlist and the plaintext rules arrive in the next commit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from app.config.settings import Settings

ALLOWED_KEYS: frozenset[str] = frozenset()
ALLOWED_EVENTS: Mapping[str, frozenset[str]] = {}
ALLOWED_RESOURCE_KEYS: frozenset[str] = frozenset()


class PlaintextRefused(ValueError):
    pass


class AllowlistExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export(spans)

    def shutdown(self) -> None:
        self.inner.shutdown()


def exporter_chain(
    settings: Settings, bind_host: str | None, inner: SpanExporter | None = None
) -> SpanExporter:
    return inner if inner is not None else ConsoleSpanExporter()


def build_tracer_provider(
    settings: Settings, bind_host: str | None, inner: SpanExporter | None = None
) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter_chain(settings, bind_host, inner)))
    return provider
