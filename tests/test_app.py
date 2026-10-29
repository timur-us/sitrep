"""The app skeleton: /health, the security headers, no redirects, the trace id, no inbound
trace context, and the default exporter chain.

Requests go through an in-process TestClient. The real server, with its logs, is in
tests/test_no_plaintext.py.
"""

from __future__ import annotations

import html
import json
import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import branding
from app.config.settings import Settings
from app.main import create_app
from app.observability import tracing

# Written out here, not imported, so that a change in the app shows up as a failure.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}
NO_STORE = {"cache-control": "no-store"}
# Set by the framework for the body, not by the app.
BODY_HEADERS = {"content-length", "content-type"}
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")
TRACE_ID = re.compile(r"[0-9a-f]{32}")
# W3C Trace Context, example values from the specification.
INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INBOUND_PARENT_ID = "00f067aa0ba902b7"


class BoomError(RuntimeError):
    """Raised by a test-only route, to reach the 500 path."""


async def _boom() -> None:
    raise BoomError("boom")


class FrozenError(RuntimeError):
    """An exception that refuses new attributes, as a frozen dataclass exception does."""

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(name)


async def _frozen() -> None:
    raise FrozenError("frozen")


async def _sets_owned_headers() -> Response:
    # A route that tries to set the headers the middleware owns.
    headers = {
        "content-security-policy": "default-src *",
        "cache-control": "public, max-age=3600",
        "x-trace-id": "0" * 32,
        "referrer-policy": "unsafe-url",
    }
    return JSONResponse({"ok": True}, headers=headers)


@pytest.fixture
def traced(settings: Settings) -> tuple[FastAPI, InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = tracing.build_tracer_provider(settings, bind_host=None, inner=memory)
    return create_app(settings, tracer_provider=provider), memory


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def _spans(memory: InMemorySpanExporter, name: str) -> list:
    return [span for span in memory.get_finished_spans() if span.name == name]


def test_health(traced: tuple[FastAPI, InMemorySpanExporter]) -> None:
    app, memory = traced
    client = _client(app)
    response = client.get("/health")
    assert response.status_code == 200, response.status_code
    assert response.json() == {"status": "ok"}

    names = [span.name for span in memory.get_finished_spans()]
    spans = _spans(memory, "GET /health")
    assert len(spans) == 1, f"exported spans: {names}"
    assert response.headers.get("x-trace-id") == format(spans[0].context.trace_id, "032x")

    again = client.get("/health")
    assert again.headers.get("x-trace-id") != response.headers.get("x-trace-id"), (
        "two requests share a trace id"
    )


@pytest.mark.parametrize(
    ("path", "status", "api"),
    [
        ("/", 200, False),
        ("/health", 200, True),
        ("/nope", 404, True),
        ("/boom", 500, True),
        ("/frozen", 500, True),
    ],
)
def test_security_headers(
    traced: tuple[FastAPI, InMemorySpanExporter], path: str, status: int, api: bool
) -> None:
    app, memory = traced
    app.add_api_route("/boom", _boom)
    app.add_api_route("/frozen", _frozen)
    response = _client(app).get(path)
    assert response.status_code == status, f"{path}: {response.status_code}"

    expected = SECURITY_HEADERS | (NO_STORE if api else {})
    for name, value in expected.items():
        assert response.headers.get_list(name) == [value], (
            f"{path}: {name} is {response.headers.get_list(name)}"
        )
    trace_ids = {format(span.context.trace_id, "032x") for span in memory.get_finished_spans()}
    assert response.headers.get("x-trace-id") in trace_ids, (
        f"{path}: x-trace-id {response.headers.get('x-trace-id')!r} is no exported span's"
    )
    extra = set(response.headers.keys()) - set(expected) - BODY_HEADERS - {"x-trace-id"}
    assert not extra, f"{path}: unexpected headers {sorted(extra)}"


def test_owned_headers_replace_route_headers(
    traced: tuple[FastAPI, InMemorySpanExporter],
) -> None:
    app, _ = traced
    app.add_api_route("/owned", _sets_owned_headers)
    response = _client(app).get("/owned")
    assert response.status_code == 200
    for name, value in (SECURITY_HEADERS | NO_STORE).items():
        assert response.headers.get_list(name) == [value], (name, response.headers.get_list(name))
    assert response.headers.get_list("x-trace-id") != ["0" * 32]
    assert len(response.headers.get_list("x-trace-id")) == 1


def test_index_title_comes_from_branding(traced: tuple[FastAPI, InMemorySpanExporter]) -> None:
    app, _ = traced
    response = _client(app).get("/")
    assert response.status_code == 200, response.status_code
    assert response.headers.get("content-type", "").startswith("text/html")
    title = html.escape(f"{branding.PRODUCT.name} — {branding.BANK.display_name}")
    assert f"<title>{title}</title>" in response.text, response.text[:300]
    assert "{{" not in response.text, "a placeholder was not filled in"


def redirects(app: FastAPI) -> tuple[set[str], int, list[str]]:
    """Every route with and without a trailing slash, plus /static, a 404 and the usual
    open-redirect shapes, under every method, with a foreign Host and forwarding headers.
    Returns the paths, the number of requests and every 3xx or Location."""
    attacker = branding.DOMAINS.attacker
    paths = {"/static", "/static/", "/static/x", "/nope", "/nope/"}
    paths |= {f"//{attacker}/", f"/%2F%2F{attacker}", "/health//", "/./health/", "/health/.."}
    for route in app.routes:
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        stem = path.rstrip("/")
        paths |= {variant for variant in (stem, stem + "/") if variant != "/" or path == "/"}
    paths.discard("")
    headers = {
        "host": branding.DOMAINS.canary,
        "x-forwarded-host": attacker,
        "x-forwarded-proto": "https",
        "forwarded": f"host={attacker};proto=https",
    }
    client = _client(app)
    count, found = 0, []
    for path in sorted(paths):
        for method in METHODS:
            # An absolute URL, so that "//host/" stays a path and is not read as a host.
            response = client.request(method, f"http://testserver{path}", headers=headers)
            count += 1
            location = response.headers.get("location")
            if 300 <= response.status_code < 400 or location is not None:
                found.append(f"{method} {path}: {response.status_code} {location}")
    return paths, count, found


def test_no_redirects(traced: tuple[FastAPI, InMemorySpanExporter]) -> None:
    app, _ = traced
    routes = {route.path for route in app.routes}
    assert {"/", "/health"} <= routes, f"routes: {sorted(routes)}"
    paths, count, found = redirects(app)
    assert {"/", "/health", "/health/"} <= paths, sorted(paths)
    assert count == len(paths) * len(METHODS) >= 13 * len(METHODS), f"only {count} requests"
    assert not found, "redirects:\n" + "\n".join(found)


def test_redirect_check_catches_a_redirect() -> None:
    # Positive control: FastAPI's default redirects a trailing slash.
    plain = FastAPI()
    plain.add_api_route("/health", lambda: {"status": "ok"})
    _, _, found = redirects(plain)
    assert any(line.startswith("GET /health/: 307") for line in found), found


@pytest.mark.parametrize(
    "traceparent",
    [
        f"00-{INBOUND_TRACE_ID}-{INBOUND_PARENT_ID}-01",
        # Unsampled: if adopted, no span would be recorded at all.
        f"00-{INBOUND_TRACE_ID}-{INBOUND_PARENT_ID}-00",
        f"00-{INBOUND_TRACE_ID}-zz-01",
    ],
)
def test_no_inbound_trace_context(
    traced: tuple[FastAPI, InMemorySpanExporter], traceparent: str
) -> None:
    app, memory = traced
    response = _client(app).get(
        "/health",
        headers={
            "traceparent": traceparent,
            "tracestate": "vendor=inbound",
            "baggage": "advisor=other",
        },
    )
    spans = _spans(memory, "GET /health")
    assert len(spans) == 1, [span.name for span in memory.get_finished_spans()]
    assert format(spans[0].context.trace_id, "032x") != INBOUND_TRACE_ID, "traceparent adopted"
    assert spans[0].parent is None, "the server span has a remote parent"
    assert not spans[0].context.trace_state, "tracestate adopted"
    values = " ".join(response.headers.values())
    assert INBOUND_TRACE_ID not in values and "inbound" not in values, response.headers


def test_default_exporter_chain(settings: Settings, capfd: pytest.CaptureFixture[str]) -> None:
    chain = tracing.exporter_chain(settings, bind_host=None)
    assert type(chain) is tracing.AllowlistExporter, f"chain is {type(chain).__name__}"
    assert type(chain.inner) is ConsoleSpanExporter, f"inner is {type(chain.inner).__name__}"

    # create_app without a provider uses that chain: the console shows a filtered span.
    canary = f"cnry{uuid.uuid4().hex}"
    response = _client(create_app(settings)).get(f"/health?q={canary}")
    assert response.status_code == 200
    out = capfd.readouterr().out
    spans = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [span["name"] for span in spans] == ["GET /health"], out[:500]
    assert canary not in out, "the query reached the console"


def test_global_tracer_provider_stays_unset(settings: Settings) -> None:
    _client(create_app(settings)).get("/health")
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider), (
        "the global tracer provider was set"
    )
