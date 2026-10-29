"""Rule 10 on the real server: no request content in spans, logs, stdout or stderr.

A real uvicorn.Server, built by server_config(), serves on 127.0.0.1 in a thread,
with an in-memory exporter behind the allowlist. A canary goes into the method, the
query, the Host header, custom and forwarding headers, the user agent, a cookie, the
referrer, the authorization header, trace context headers, a body, a 404 path and an
exception message. OpenTelemetry's own switches that copy request and response headers,
resource attributes or raw methods into spans are turned on, so the exporter, not the
configuration, has to keep them out.

The control runs the same requests against the same app with an unfiltered exporter and
uvicorn's default logging: there the canary does appear, so the checks can fail.
"""

from __future__ import annotations

import errno
import http.client
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
import uvicorn
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceState

from app.__main__ import server_config
from app.config import branding
from app.config.settings import Settings
from app.main import create_app
from app.observability import tracing
from app.observability.logs import LOG_CONFIG, SafeFormatter, StderrHandler

# The reviewed allowlists, written out here so that widening one is a visible change.
# The keys are the instrumentation's default (pre-stable) HTTP conventions; target,
# URL, host, server name, user agent and peer address are left out on purpose.
ALLOWED_KEYS = {"http.method", "http.route", "http.status_code", "http.scheme", "http.flavor"}
ALLOWED_EVENTS = {"exception": {"exception.type"}}
ALLOWED_RESOURCE_KEYS = {"service.name"}
# Values the allowed keys may take here: none of them carries request text.
ALLOWED_VALUES = {
    "http.method": {"GET", "POST", "_OTHER"},
    "http.route": {"/health", "/boom"},
    "http.scheme": {"http"},
    "http.flavor": {"1.1"},
}

CAPTURE_HEADERS = (
    "x-canary,cookie,user-agent,host,referer,content-type,authorization,"
    "x-forwarded-for,x-forwarded-proto,forwarded,tracestate,baggage,x-trace-id"
)
WAIT = 5.0


class CanaryError(RuntimeError):
    """Raised with the canary as its message."""


# How OpenTelemetry writes exception.type, and the logs with it: module and name.
CANARY_ERROR = f"{CanaryError.__module__}.{CanaryError.__qualname__}"


@dataclass
class Running:
    canary: str
    port: int
    memory: InMemorySpanExporter


def _wait_for(condition: Callable[[], object], what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


@pytest.fixture(params=["redacted", "control"])
def running(
    request: pytest.FixtureRequest,
    settings: Settings,
    restore_logging: None,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Running]:
    canary = f"cnry{uuid.uuid4().hex}"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", CAPTURE_HEADERS)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE", CAPTURE_HEADERS)
    monkeypatch.setenv("OTEL_PYTHON_INSTRUMENTATION_HTTP_CAPTURE_ALL_METHODS", "1")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"leak={canary}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", canary)

    memory = InMemorySpanExporter()
    local = settings.model_copy(update={"port": 0})
    # Built here, in setup: uvicorn.Config applies the log config, which replaces the
    # root handlers, and pytest adds its capturing handler for the test call afterwards.
    if request.param == "redacted":
        provider = tracing.build_tracer_provider(settings, bind_host=None, inner=memory)
        config = server_config(local, tracer_provider=provider)
    else:
        # The same app with an unfiltered exporter, uvicorn's default logging and its
        # access log: what the rule-10 checks are measured against.
        provider = TracerProvider(resource=Resource.create())
        provider.add_span_processor(SimpleSpanProcessor(memory))
        app = create_app(local, tracer_provider=provider)
        config = uvicorn.Config(app, host=local.host, port=0, http="h11", ws="none")

    async def boom() -> None:
        raise CanaryError(canary)

    config.app.add_api_route("/boom", boom)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for(lambda: server.started, "the server to start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield Running(canary, port, memory)
    finally:
        server.should_exit = True
        thread.join(WAIT)
    assert not thread.is_alive(), "the server thread did not stop"


def _request(
    port: int, method: str, target: str, headers: dict[str, str], body: str | None = None
) -> tuple[int, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}
    finally:
        connection.close()


def _send_canaries(running: Running) -> list[tuple[str, int, dict[str, str]]]:
    """Every request, as (label, status, response headers)."""
    canary = running.canary
    headers = {
        "Host": f"{canary}.{branding.DOMAINS.canary}",
        "X-Canary": canary,
        "User-Agent": canary,
        "Cookie": f"session={canary}",
        "Referer": f"https://{branding.DOMAINS.canary}/{canary}",
        "Authorization": f"Bearer {canary}",
        "X-Forwarded-For": f"{canary}.{branding.DOMAINS.attacker}",
        "X-Forwarded-Proto": canary,
        "Forwarded": f"for={canary};proto={canary}",
        "tracestate": f"vendor={canary}",
        "baggage": f"advisor={canary}",
    }
    sent = []
    for method, target, body in [
        ("GET", f"/health?q={canary}", None),
        ("GET", f"/{canary}?d={canary}", None),
        ("GET", f"/boom?q={canary}", None),
        ("POST", f"/health?q={canary}", canary),
        (canary.upper(), f"/health?q={canary}", None),
    ]:
        status, response_headers = _request(running.port, method, target, headers, body)
        label = f"{method} {target.split('?')[0]}".replace(canary.upper(), "<CANARY>")
        sent.append((label.replace(canary, "<canary>"), status, response_headers))
    return sent


def _check_span(span: ReadableSpan, canary: str) -> list[str]:
    problems = []
    if canary.lower() in span.to_json().lower():
        problems.append(f"{span.name}: canary in the serialised span")
    attributes = dict(span.attributes or {})
    if unknown := set(attributes) - ALLOWED_KEYS:
        problems.append(f"{span.name}: attribute keys {sorted(unknown)}")
    for key, values in ALLOWED_VALUES.items():
        if key in attributes and attributes[key] not in values:
            problems.append(f"{span.name}: {key} is {attributes[key]!r}")
    for event in span.events:
        allowed = ALLOWED_EVENTS.get(event.name)
        if allowed is None or set(event.attributes or {}) - allowed:
            problems.append(f"{span.name}: event {event.name} {sorted(event.attributes or {})}")
    if span.status.description is not None:
        problems.append(f"{span.name}: status description kept")
    if unknown := set(span.resource.attributes) - ALLOWED_RESOURCE_KEYS:
        problems.append(f"{span.name}: resource keys {sorted(unknown)}")
    if span.links:
        problems.append(f"{span.name}: links kept")
    if span.context.trace_state:
        problems.append(f"{span.name}: trace state kept")
    return problems


def test_exporter_allowlists_are_the_reviewed_ones() -> None:
    assert tracing.ALLOWED_KEYS == ALLOWED_KEYS
    assert {name: set(keys) for name, keys in tracing.ALLOWED_EVENTS.items()} == ALLOWED_EVENTS
    assert tracing.ALLOWED_RESOURCE_KEYS == ALLOWED_RESOURCE_KEYS


@pytest.mark.parametrize("running", ["redacted"], indirect=True)
def test_no_plaintext_in_traces_or_logs(
    running: Running, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    canary = running.canary
    for name in (None, *LOG_CONFIG.get("loggers", {})):
        caplog.set_level(logging.DEBUG, logger=name)
    sent = _send_canaries(running)
    assert [(label, status) for label, status, _ in sent] == [
        ("GET /health", 200),
        ("GET /<canary>", 404),
        ("GET /boom", 500),
        ("POST /health", 405),
        ("<CANARY> /health", 405),
    ], sent
    for label, _, response_headers in sent:
        assert "server" not in response_headers, f"{label}: server header sent"

    # Spans: one server span per request, the failed one recorded by exception type only,
    # and each response's X-Trace-Id is its own span's trace id.
    _wait_for(lambda: len(running.memory.get_finished_spans()) >= len(sent), "the spans")
    spans = running.memory.get_finished_spans()
    problems = [problem for span in spans for problem in _check_span(span, canary)]
    assert not problems, "\n".join(problems)
    assert sorted(format(span.context.trace_id, "032x") for span in spans) == sorted(
        response_headers.get("x-trace-id", "") for _, _, response_headers in sent
    ), "a response's X-Trace-Id is not its span's trace id"
    assert sorted(span.name for span in spans) == sorted(
        ["GET /health", "GET", "GET /boom", "POST /health", tracing.REDACTED_NAME]
    ), [span.name for span in spans]
    failed = [span for span in spans if span.status.status_code is StatusCode.ERROR]
    assert [span.name for span in failed] == ["GET /boom"], [span.name for span in spans]
    assert [(event.name, dict(event.attributes or {})) for event in failed[0].events] == [
        ("exception", {"exception.type": CANARY_ERROR})
    ]
    trace_id = format(failed[0].context.trace_id, "032x")

    # Log records, as every handler receives them, including the exception record. pytest
    # attaches its handler to non-propagating loggers too, so uvicorn.access records
    # appear here although the server itself has no access handler (test_server_config).
    def logged() -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.name == "uvicorn.error" and getattr(record, "exc_type", None) == CANARY_ERROR
        ]

    _wait_for(logged, "uvicorn's exception record")
    assert logged()[0].trace_id == trace_id, "the exception record has another trace id"
    for record in caplog.records:
        assert canary not in record.getMessage().lower(), f"{record.name}: canary in the message"
        assert record.exc_info is None and record.exc_text is None, f"{record.name}: exception"
        assert record.stack_info is None, f"{record.name}: stack"
        assert canary not in repr(vars(record)).lower(), f"{record.name}: canary in the record"
    assert canary not in caplog.text.lower()

    # Standard output and error, as written by the log handler and anything else. The
    # stderr handler writes the exception line before pytest's capturing handler sees
    # the record (handlers run in order), so the record above implies the line below.
    out, err = capfd.readouterr()
    assert canary not in out.lower(), "canary on stdout"
    assert canary not in err.lower(), "canary on stderr"
    assert f"exc_type={CANARY_ERROR} trace_id={trace_id}" in err, err[-1000:]


@pytest.mark.parametrize("running", ["control"], indirect=True)
def test_canary_reaches_unfiltered_spans_and_default_logs(
    running: Running, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    # Control: without the allowlist and the log config, the same requests leak. This
    # proves the canary, the capture variables and the exception path are live.
    canary = running.canary
    sent = _send_canaries(running)
    assert [status for _, status, _ in sent] == [200, 404, 500, 405, 405], sent
    _wait_for(lambda: len(running.memory.get_finished_spans()) >= len(sent), "the spans")
    spans = running.memory.get_finished_spans()
    leaking = {key for span in spans for key, value in (span.attributes or {}).items()}
    leaking = {key for key in leaking if any(canary in str(v) for v in _values(spans, key))}
    assert {"http.target", "http.url", "http.request.header.x_canary"} <= leaking, leaking
    assert any(canary.upper() in span.name for span in spans), "the raw method is not in a name"
    assert any(
        canary in (event.attributes or {}).get("exception.message", "")
        for span in spans
        for event in span.events
    ), "the exception message is not in an event"
    _wait_for(lambda: canary in caplog.text, "the canary in a log record")
    _, err = capfd.readouterr()
    assert canary in err, "the canary is not on stderr with uvicorn's default logging"


def _values(spans: tuple[ReadableSpan, ...], key: str) -> list[object]:
    return [(span.attributes or {})[key] for span in spans if key in (span.attributes or {})]


def _span(**overrides: object) -> ReadableSpan:
    context = SpanContext(trace_id=1, span_id=2, is_remote=False)
    fields: dict = {
        "name": "GET /health",
        "context": context,
        "attributes": {"http.method": "GET", "http.route": "/health", "http.status_code": 200},
        "kind": SpanKind.SERVER,
        "resource": Resource({"service.name": "x"}),
    }
    return ReadableSpan(**(fields | overrides))


def test_redact_drops_everything_unapproved() -> None:
    canary = f"cnry{uuid.uuid4().hex}"
    state = TraceState([("vendor", canary)])
    span = _span(
        context=SpanContext(trace_id=1, span_id=2, is_remote=False, trace_state=state),
        parent=SpanContext(trace_id=1, span_id=5, is_remote=True, trace_state=state),
        name=f"GET /{canary}",
        attributes={
            "http.method": canary.upper(),
            "http.route": "/health",
            "http.target": f"/health?q={canary}",
            "http.status_code": 500,
        },
        events=[
            Event(
                "exception",
                {
                    "exception.type": "RuntimeError",
                    "exception.message": canary,
                    "exception.stacktrace": canary,
                },
            ),
            Event(canary, {"note": canary}),
        ],
        links=[Link(SpanContext(trace_id=3, span_id=4, is_remote=True), {"note": canary})],
        resource=Resource({"service.name": "x", "leak": canary}),
        status=Status(StatusCode.ERROR, f"RuntimeError: {canary}"),
    )
    redacted = tracing.redact(span)
    assert canary.lower() not in redacted.to_json().lower(), redacted.to_json()
    assert redacted.name == tracing.REDACTED_NAME
    assert dict(redacted.attributes) == {
        "http.method": "_OTHER",
        "http.route": "/health",
        "http.status_code": 500,
    }
    assert [(e.name, dict(e.attributes)) for e in redacted.events] == [
        ("exception", {"exception.type": "RuntimeError"})
    ]
    assert redacted.links == ()
    assert dict(redacted.resource.attributes) == {"service.name": "x"}
    assert redacted.status.status_code is StatusCode.ERROR
    assert redacted.status.description is None
    assert (redacted.context.trace_id, redacted.context.span_id) == (1, 2)
    assert (redacted.parent.trace_id, redacted.parent.span_id) == (1, 5)
    assert not redacted.context.trace_state and not redacted.parent.trace_state


@pytest.mark.parametrize(
    ("name", "method", "route", "kept"),
    [
        ("GET /health", "GET", "/health", True),
        ("GET", "GET", None, True),
        ("HTTP /health", "_OTHER", "/health", True),
        ("PATCH /health", "patch", "/health", False),
        ("GET /other", "GET", "/health", False),
        ("GET /health", None, "/health", False),
    ],
)
def test_span_name_is_method_and_route_only(
    name: str, method: str | None, route: str | None, kept: bool
) -> None:
    attributes = {key: value for key, value in (("http.method", method), ("http.route", route))}
    span = _span(name=name, attributes={k: v for k, v in attributes.items() if v is not None})
    assert tracing.redact(span).name == (name if kept else tracing.REDACTED_NAME)


def test_trace_ids_come_from_a_secure_generator(settings: Settings) -> None:
    provider = tracing.build_tracer_provider(settings, bind_host=None, inner=InMemorySpanExporter())
    generator = provider.id_generator
    assert type(generator) is tracing.SecureIdGenerator, type(generator).__name__
    trace_ids = {generator.generate_trace_id() for _ in range(64)}
    assert len(trace_ids) == 64 and 0 not in trace_ids
    assert all(0 < generator.generate_span_id() < 2**64 for _ in range(64))


def test_server_config(settings: Settings, restore_logging: None) -> None:
    config = server_config(settings)
    assert (config.host, config.port) == ("127.0.0.1", 8000)
    assert config.access_log is False, "access log on"
    assert config.proxy_headers is False, "proxy headers trusted"
    assert config.server_header is False, "server header sent"
    assert (config.http, config.ws) == ("h11", "none")
    assert config.log_config is LOG_CONFIG, "not the redacting log config"
    # uvicorn writes an access line only when its access logger reaches a handler.
    assert not logging.getLogger("uvicorn.access").hasHandlers(), "access records are handled"


def test_log_config_names_only_reviewed_classes() -> None:
    # dictConfig imports these by name; a logging.handlers class would ship logs away.
    named = [
        value
        for section in ("filters", "formatters", "handlers")
        for entry in LOG_CONFIG.get(section, {}).values()
        for key, value in entry.items()
        if key in ("()", "class")
    ]
    assert len(named) == 3, named
    assert all(value.startswith("app.observability.logs.") for value in named), named


def test_logged_exception_object_shows_type_and_errno_only() -> None:
    # uvicorn logs a failed bind as logger.error(exc): the record's message is the
    # exception itself, and str() of it would be its text.
    canary = f"cnry{uuid.uuid4().hex}"
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, OSError(errno.EADDRINUSE, canary), (), None
    )
    line = SafeFormatter().format(record)
    assert canary not in line, line
    assert line.endswith(
        " ERROR uvicorn.error OSError exc_type=OSError errno=EADDRINUSE trace_id=-"
    )
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, CanaryError(canary), (), None)
    line = SafeFormatter().format(record)
    assert canary not in line, line
    assert line.endswith(f" ERROR x {CANARY_ERROR} exc_type={CANARY_ERROR} trace_id=-"), line


def test_formatter_redacts_records_from_any_logger() -> None:
    # The handler is the backstop for loggers without the filter, asyncio for one.
    canary = f"cnry{uuid.uuid4().hex}"
    try:
        raise CanaryError(canary)
    except CanaryError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "asyncio", logging.ERROR, __file__, 1, "task failed: %s", (canary,), exc_info
    )
    line = SafeFormatter().format(record)
    assert canary not in line, line
    assert f"asyncio task failed: %s exc_type={CANARY_ERROR}" in line, line


class _BreaksOnce:
    """A stderr whose first write fails, as a closed or full pipe would."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.broken = True

    def write(self, text: str) -> int:
        if self.broken:
            self.broken = False
            raise ValueError("I/O operation on closed file.")
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        return None


@pytest.mark.parametrize("safe", [True, False])
def test_failed_log_write_prints_no_exception(monkeypatch: pytest.MonkeyPatch, safe: bool) -> None:
    # logging's handleError prints a traceback whose context is the exception being
    # logged. The stock handler (the control) leaks its message; StderrHandler does not.
    canary = f"cnry{uuid.uuid4().hex}"
    stream = _BreaksOnce()
    monkeypatch.setattr(sys, "stderr", stream)
    handler = StderrHandler() if safe else logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter())
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "failed", (), None)
    try:
        raise CanaryError(canary)
    except CanaryError:
        record.exc_info = sys.exc_info()
        handler.handle(record)
    leaked = any(canary in text for text in stream.written)
    assert leaked is not safe, f"safe={safe}: written {stream.written}"
    if safe:
        assert stream.written == ["logging error: a log line was dropped\n"], stream.written
