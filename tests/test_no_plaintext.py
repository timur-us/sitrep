"""Rule 10 on the real server: no request content in spans, logs, stdout or stderr.

A real uvicorn.Server, built by server_config(), serves on 127.0.0.1 in a thread,
with an in-memory exporter behind the allowlist. A canary goes into the query, the
Host header, a custom header, the user agent, a cookie, the referrer, a body, a 404
path and an exception message. OpenTelemetry's own switches that copy request and
response headers or resource attributes into spans are turned on, so the exporter,
not the configuration, has to keep them out.
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
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.__main__ import server_config
from app.config import branding
from app.config.settings import Settings
from app.observability import tracing
from app.observability.logs import LOG_CONFIG, SafeFormatter, StderrHandler

# The reviewed allowlists, written out here so that widening one is a visible change.
# The keys are the instrumentation's default (pre-stable) HTTP conventions; target,
# URL, host, server name, user agent and peer address are left out on purpose.
ALLOWED_KEYS = {"http.method", "http.route", "http.status_code", "http.scheme", "http.flavor"}
ALLOWED_EVENTS = {"exception": {"exception.type"}}
ALLOWED_RESOURCE_KEYS = {"service.name"}

CAPTURE_HEADERS = "x-canary,cookie,user-agent,host,referer,content-type,x-trace-id"
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


@pytest.fixture
def running(
    settings: Settings,
    restore_logging: None,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Running]:
    canary = f"cnry{uuid.uuid4().hex}"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", CAPTURE_HEADERS)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE", CAPTURE_HEADERS)
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"leak={canary}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", canary)

    memory = InMemorySpanExporter()
    provider = tracing.build_tracer_provider(settings, bind_host=None, inner=memory)
    # Built here, in setup: uvicorn.Config applies the log config, which replaces the
    # root handlers, and pytest adds its capturing handler for the test call afterwards.
    config = server_config(settings.model_copy(update={"port": 0}), tracer_provider=provider)

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


def _check_span(span: ReadableSpan, canary: str) -> list[str]:
    problems = []
    if canary in span.to_json():
        problems.append(f"{span.name}: canary in the serialised span")
    if unknown := set(span.attributes or {}) - ALLOWED_KEYS:
        problems.append(f"{span.name}: attribute keys {sorted(unknown)}")
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
    return problems


def test_exporter_allowlists_are_the_reviewed_ones() -> None:
    assert tracing.ALLOWED_KEYS == ALLOWED_KEYS
    assert {name: set(keys) for name, keys in tracing.ALLOWED_EVENTS.items()} == ALLOWED_EVENTS
    assert tracing.ALLOWED_RESOURCE_KEYS == ALLOWED_RESOURCE_KEYS


def test_no_plaintext_in_traces_or_logs(
    running: Running, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    canary, port = running.canary, running.port
    for name in (None, *LOG_CONFIG.get("loggers", {})):
        caplog.set_level(logging.DEBUG, logger=name)
    headers = {
        "Host": f"{canary}.{branding.DOMAINS.canary}",
        "X-Canary": canary,
        "User-Agent": canary,
        "Cookie": f"session={canary}",
        "Referer": f"https://{branding.DOMAINS.canary}/{canary}",
    }
    statuses = {}
    for method, target, body in [
        ("GET", f"/health?q={canary}", None),
        ("GET", f"/{canary}?d={canary}", None),
        ("GET", f"/boom?q={canary}", None),
        ("POST", f"/health?q={canary}", canary),
    ]:
        status, response_headers = _request(port, method, target, headers, body)
        statuses[method, target.split("?")[0].replace(canary, "<canary>")] = status
        assert "server" not in response_headers, f"{target}: server header sent"
        assert "x-trace-id" in response_headers, f"{target}: no x-trace-id"
    assert statuses == {
        ("GET", "/health"): 200,
        ("GET", "/<canary>"): 404,
        ("GET", "/boom"): 500,
        ("POST", "/health"): 405,
    }, statuses

    # Spans: four server spans, the failed one recorded by exception type only.
    _wait_for(lambda: len(running.memory.get_finished_spans()) >= 4, "four spans")
    spans = running.memory.get_finished_spans()
    problems = [problem for span in spans for problem in _check_span(span, canary)]
    assert not problems, "\n".join(problems)
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
        assert canary not in record.getMessage(), f"{record.name}: canary in the message"
        assert record.exc_info is None and record.exc_text is None, f"{record.name}: exception"
        assert record.stack_info is None, f"{record.name}: stack"
        assert canary not in repr(vars(record)), f"{record.name}: canary in the record"
    assert canary not in caplog.text

    # Standard output and error, as written by the log handler and anything else.
    out, err = capfd.readouterr()
    assert canary not in out, "canary on stdout"
    assert canary not in err, "canary on stderr"
    assert f"exc_type={CANARY_ERROR} trace_id={trace_id}" in err, err[-1000:]


def test_server_config(settings: Settings, restore_logging: None) -> None:
    config = server_config(settings)
    assert (config.host, config.port) == ("127.0.0.1", 8000)
    assert config.access_log is False, "access log on"
    assert config.proxy_headers is False, "proxy headers trusted"
    assert config.server_header is False, "server header sent"
    assert config.log_config is LOG_CONFIG, "not the redacting log config"
    # uvicorn writes an access line only when its access logger reaches a handler.
    assert not logging.getLogger("uvicorn.access").hasHandlers(), "access records are handled"


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
