"""Tracing without plaintext (CLAUDE.md rule 10, ADR 0007).

Every span passes AllowlistExporter on its way out. It keeps approved attribute keys,
approved events with approved attributes, approved resource attributes and the status
code; everything else is dropped, whatever the instrumentation or its environment
variables put in. Span names are kept only when they are the method and the route
template the instrumentation derived them from.

The global tracer provider is never set: the provider goes to the instrumentation
explicitly. The global propagator is an empty one, so an inbound traceparent, tracestate
or baggage header is never adopted.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from fastapi import FastAPI
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status

from app.config import branding
from app.config.settings import LOOPBACK, Settings

# The instrumentation's default (pre-stable) HTTP conventions. Left out on purpose:
# http.target and http.url (path and query), http.host and http.server_name (the Host
# header), http.user_agent, net.peer.* (the client), and captured headers.
ALLOWED_KEYS = frozenset(
    {"http.method", "http.route", "http.status_code", "http.scheme", "http.flavor"}
)
# exception.message and exception.stacktrace carry the text; the type does not.
ALLOWED_EVENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"exception": frozenset({"exception.type"})}
)
ALLOWED_RESOURCE_KEYS = frozenset({"service.name"})
# The name of a span whose name is not method plus route.
REDACTED_NAME = "redacted"


class PlaintextRefused(ValueError):
    """TRACE_PLAINTEXT=1 where it is not allowed. The message names no value."""


class EmptyPropagator(TextMapPropagator):
    """Extracts nothing and injects nothing: inbound trace context is never adopted.

    An empty CompositePropagator would return no context at all, which breaks span
    creation; this returns a fresh, empty one.
    """

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return context if context is not None else Context()

    def inject(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        return None

    @property
    def fields(self) -> set[str]:
        return set()


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return trace.format_trace_id(context.trace_id) if context.is_valid else None


def _keep(attributes: Mapping[str, object] | None, allowed: frozenset[str]) -> dict:
    return {key: value for key, value in (attributes or {}).items() if key in allowed}


def _name(span: ReadableSpan) -> str:
    attributes = span.attributes or {}
    method, route = attributes.get("http.method"), attributes.get("http.route")
    # The instrumentation names a span "<method> <route>", or "<method>" without a
    # route, and uses "HTTP" for methods it does not know.
    names = {f"{verb} {route}" if route else str(verb) for verb in (method, "HTTP") if verb}
    return span.name if span.name in names else REDACTED_NAME


def redact(span: ReadableSpan) -> ReadableSpan:
    events = [
        Event(event.name, _keep(event.attributes, ALLOWED_EVENTS[event.name]), event.timestamp)
        for event in span.events
        if event.name in ALLOWED_EVENTS
    ]
    return ReadableSpan(
        name=_name(span),
        context=span.context,
        parent=span.parent,
        resource=Resource(_keep(span.resource.attributes, ALLOWED_RESOURCE_KEYS)),
        attributes=_keep(span.attributes, ALLOWED_KEYS),
        events=events,
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class AllowlistExporter(SpanExporter):
    """Passes each span through redact() to the inner exporter."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([redact(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def _console() -> ConsoleSpanExporter:
    # One JSON object per line, to the stdout of the moment (not of import time).
    return ConsoleSpanExporter(out=sys.stdout, formatter=lambda span: span.to_json(None) + "\n")


def exporter_chain(
    settings: Settings, bind_host: str | None, inner: SpanExporter | None = None
) -> SpanExporter:
    """AllowlistExporter(inner), inner defaulting to the console.

    With TRACE_PLAINTEXT=1 the console exporter is returned unwrapped, and only when the
    entry point binds exactly 127.0.0.1. Anything else raises PlaintextRefused.
    """
    exporter = _console() if inner is None else inner
    if not settings.trace_plaintext_on:
        return AllowlistExporter(exporter)
    if type(exporter) is not ConsoleSpanExporter:
        raise PlaintextRefused("TRACE_PLAINTEXT=1 is allowed with the console exporter only")
    if bind_host != LOOPBACK:
        raise PlaintextRefused(
            "TRACE_PLAINTEXT=1 is allowed only when the entry point binds 127.0.0.1"
        )
    return exporter


def build_tracer_provider(
    settings: Settings, bind_host: str | None, inner: SpanExporter | None = None
) -> TracerProvider:
    # A plain Resource: Resource.create() would merge OTEL_RESOURCE_ATTRIBUTES and
    # OTEL_SERVICE_NAME from the environment. Spans export as they end (local console).
    provider = TracerProvider(resource=Resource({"service.name": branding.SLUG}))
    provider.add_span_processor(SimpleSpanProcessor(exporter_chain(settings, bind_host, inner)))
    return provider


def instrument(app: FastAPI, provider: TracerProvider) -> None:
    propagate.set_global_textmap(EmptyPropagator())
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        meter_provider=NoOpMeterProvider(),
        # An explicit empty list, so OTEL_PYTHON_FASTAPI_EXCLUDED_URLS cannot switch
        # tracing, and with it X-Trace-Id, off for a route.
        excluded_urls="",
        exclude_spans=["receive", "send"],
    )
