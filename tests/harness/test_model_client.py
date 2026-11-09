"""The model client (M1 PR 6, ADR 0012): one factory, built from settings.

- Every SDK variable but the key is refused, set or empty, in any case. The list is read
  from the installed SDK's source, so an SDK upgrade that adds one is checked here.
- A canary key reaches no repr, log record, span or error, even when the server echoes it.
- A request carries the registry's tool schemas as given, no tool_choice and the model
  from settings, to the constant base URL, with no retries, no redirects and a timeout.
- Proxy variables are not used, and no socket opens through the mock transport.
- Cost is usage times the dated price table.

No test reaches the network: the requests go to httpx2.MockTransport, and the one real
transport meets a loopback listener and a stubbed DNS lookup.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import traceback
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
import uvicorn
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app import __main__ as entry
from app.config import branding
from app.config.settings import DEFAULT_MODEL, KEY_VARIABLE, Settings, env_name
from app.harness import model_client, mvp_tools
from app.harness.model_client import (
    BASE_URL,
    ModelCallError,
    ModelClientRefused,
    PriceUnknown,
    build_model_client,
    check_start,
    cost_usd,
    refused_variables,
)

MODEL = env_name("model")
MESSAGES_URL = f"{BASE_URL}/v1/messages"
SYSTEM = "You prepare an advisor for a client call."
PROXY_VARIABLES = tuple(
    name for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for name in (base, base.lower())
)


def canary(kind: str) -> str:
    # Not key-shaped on purpose: the secret scanners and the hook look for real key shapes.
    return f"canary-{kind}-{secrets.token_hex(12)}"


def sdk_variables() -> set[str]:
    """Every ANTHROPIC_* name written as a string literal in the installed SDK."""
    root = Path(anthropic.__file__).parent
    pattern = re.compile(r"""["'](ANTHROPIC_[A-Z0-9_]+)["']""")
    return {
        name
        for path in root.rglob("*.py")
        for name in pattern.findall(path.read_text(encoding="utf-8"))
    }


SDK_VARIABLES = sorted(sdk_variables())


def message_body(model: str = DEFAULT_MODEL, **fields: Any) -> dict[str, Any]:
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return body | fields


class Recorder:
    """A MockTransport handler that records each request and answers with respond()."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None):
        self.requests: list[httpx2.Request] = []
        self.respond = respond or (lambda request: httpx2.Response(200, json=message_body()))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self.respond(request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def ask(client: model_client.AnthropicModelClient, text: str = "Hello", **kwargs: Any) -> Any:
    kwargs.setdefault("tools", [])
    return client.create(
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
        max_tokens=1024,
        **kwargs,
    )


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = canary("key")
    monkeypatch.setenv(KEY_VARIABLE, value)
    return value


@pytest.fixture
def keyed(key: str) -> Settings:
    return Settings(_env_file=None)


# --- refused variables -------------------------------------------------------------


def test_the_sdk_source_is_read() -> None:
    # Non-vacuity: the names verified while building (anthropic 2.3.0) are found, so the
    # scan reads the SDK. Each one redirects, authenticates, logs or traces the client.
    expected = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_LOG",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_OPEN_TELEMETRY",
        "ANTHROPIC_OPEN_TELEMETRY_PROPAGATION",
        "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE",
        "ANTHROPIC_OPEN_TELEMETRY_TRACES_MAX_CONTENT_BYTES",
    }
    assert expected <= set(SDK_VARIABLES), sorted(expected - set(SDK_VARIABLES))


def test_every_sdk_variable_but_the_key_is_refused() -> None:
    names = set(SDK_VARIABLES) | {"ANTHROPIC_NOT_YET_IN_THE_SDK", "ANTHROPIC_MODEL"}
    environ = dict.fromkeys(names, "")
    assert refused_variables(environ) == sorted(names - {KEY_VARIABLE})
    # In any case, as the settings read names; an unrelated name is not refused.
    assert refused_variables({"anthropic_base_url": "x", "Anthropic_Log": "debug"}) == [
        "Anthropic_Log",
        "anthropic_base_url",
    ]
    assert refused_variables({KEY_VARIABLE: "x", KEY_VARIABLE.lower(): "x", "HOME": "/"}) == []


# Review finding test-8: what the SDK and its HTTP library read from the environment
# outside the prefix, excluding anthropic/lib (other clients, banned in app/). Each is
# harmless with this client: DEFER_PYDANTIC_BUILD only times the SDK's model builds, and
# SSL_CERT_FILE and SSL_CERT_DIR are read only with trust_env, which is off.
UNPREFIXED_READS = frozenset({"DEFER_PYDANTIC_BUILD", "SSL_CERT_FILE", "SSL_CERT_DIR"})
# Proxy variables are read through urllib's getproxies(), only in these two files: the
# SDK's DefaultHttpxClient (banned in app/) and httpx2 under trust_env.
GETPROXIES_FILES = frozenset({"anthropic/_utils/_httpx.py", "httpx2/_utils.py"})


def environment_reads() -> tuple[set[str], set[str]]:
    """(names read by literal outside the prefix, files that call getproxies())."""
    site = Path(anthropic.__file__).parent.parent
    # os.environ and os.getenv only: httpx2's WSGI transport fills a request environ
    # (CONTENT_LENGTH), which is not the process environment.
    read = re.compile(
        r"""(?:os\.environ\.get\(|os\.environ\[|getenv\()\s*["']([A-Za-z0-9_]+)["']"""
    )
    alias = re.compile(r"from os import[^\n]*\benviron\b|import os as ")
    names: set[str] = set()
    proxies: set[str] = set()
    for top in ("anthropic", "httpx2", "httpcore2"):
        for path in (site / top).rglob("*.py"):
            rel = path.relative_to(site).as_posix()
            if rel.startswith("anthropic/lib/"):
                continue
            text = path.read_text(encoding="utf-8")
            names |= {n for n in read.findall(text) if not n.startswith("ANTHROPIC_")}
            # A renamed os would hide reads from the pattern above.
            assert not alias.search(text), rel
            if "getproxies()" in text:
                proxies.add(rel)
    return names, proxies


def test_the_unprefixed_environment_reads_are_known() -> None:
    names, proxies = environment_reads()
    assert names == UNPREFIXED_READS, sorted(names)
    assert proxies == GETPROXIES_FILES, sorted(proxies)


@pytest.mark.parametrize("name", [n for n in SDK_VARIABLES if n != KEY_VARIABLE])
@pytest.mark.parametrize("value", ["set", ""])
def test_the_factory_refuses_each_sdk_variable(
    monkeypatch: pytest.MonkeyPatch, keyed: Settings, name: str, value: str
) -> None:
    planted = f"{value}-{canary('value')}" if value else ""
    monkeypatch.setenv(name, planted)
    with pytest.raises(ModelClientRefused) as refused:
        build_model_client(keyed, transport=Recorder().transport())
    message = str(refused.value)
    assert name in message, message
    assert not planted or planted not in message, message


def test_an_old_model_variable_names_the_new_one(
    monkeypatch: pytest.MonkeyPatch, keyed: Settings
) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
    with pytest.raises(ModelClientRefused, match=MODEL):
        check_start(keyed, {"ANTHROPIC_MODEL": DEFAULT_MODEL})


def test_the_key_alone_is_accepted(keyed: Settings) -> None:
    check_start(keyed, {KEY_VARIABLE: "x"})
    assert repr(build_model_client(keyed)) == f"AnthropicModelClient(model={DEFAULT_MODEL!r})"


def test_the_factory_needs_a_key(settings: Settings) -> None:
    assert settings.anthropic_api_key is None
    with pytest.raises(ModelClientRefused, match=KEY_VARIABLE):
        build_model_client(settings)


def test_a_model_without_a_price_is_refused(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    unknown = f"claude-{canary('model')}"
    monkeypatch.setenv(MODEL, unknown)
    settings = Settings(_env_file=None)
    assert settings.model == unknown
    for call in (lambda: check_start(settings, {}), lambda: build_model_client(settings)):
        with pytest.raises(ModelClientRefused) as refused:
            call()
        assert MODEL in str(refused.value) and unknown not in str(refused.value)


# --- the entry point ------------------------------------------------------------------


def _no_server(self: uvicorn.Server, sockets: list | None = None) -> None:
    raise AssertionError("the server started; the entry point should have refused")


def _no_bind(self: uvicorn.Config) -> socket.socket:
    raise AssertionError("the entry point bound a socket before refusing")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANTHROPIC_BASE_URL", f"https://{branding.DOMAINS.attacker}/{canary('url')}"),
        ("ANTHROPIC_AUTH_TOKEN", ""),
        ("ANTHROPIC_LOG", canary("level")),
        ("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", canary("mode")),
        ("ANTHROPIC_MODEL", canary("old-model")),
        (env_name("model"), canary("model")),
        # Review finding sec-1: an exported key would win over the one in the dotenv
        # file, the workspace key with its spend limit. Set, even empty, it is refused.
        (KEY_VARIABLE, canary("exported-key")),
        (KEY_VARIABLE.lower(), ""),
    ],
)
@pytest.mark.usefixtures("restore_logging")
def test_the_entry_point_refuses_before_it_binds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.setattr(uvicorn.Config, "bind_socket", _no_bind)
    monkeypatch.setattr(uvicorn.Server, "run", _no_server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, value)
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "refused to start" in err and name in err, err
    assert not value or value not in err, err


@pytest.mark.parametrize("shape", ["{} ", "a b{}", "{}é"])
@pytest.mark.usefixtures("restore_logging")
def test_the_entry_point_refuses_a_bad_key_unseen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    # Review finding sec-3: a ValidationError keeps its input in errors() and json(), and
    # uncaught it ends in a traceback. The entry point names the field only, and exits 2.
    key = shape.format(canary("bad-key"))
    monkeypatch.setattr(uvicorn.Config, "bind_socket", _no_bind)
    monkeypatch.setattr(uvicorn.Server, "run", _no_server)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f'{KEY_VARIABLE}="{key}"\n', encoding="utf-8")
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "refused to start" in err and KEY_VARIABLE in err, err
    assert key.strip() not in err and "Traceback" not in err, err


@pytest.mark.usefixtures("restore_logging")
def test_the_entry_point_starts_with_a_key_and_no_sdk_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str
) -> None:
    # Control for the refusals above: the key itself, from .env, starts the app.
    started: list[bool] = []
    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda config: socket.socket())
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: started.append(True))
    monkeypatch.delenv(KEY_VARIABLE)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{KEY_VARIABLE}={key}\n{MODEL}={DEFAULT_MODEL}\n")
    assert entry.main() == 0
    assert started == [True]


# --- the canary key ---------------------------------------------------------------------


def _echo_key(request: httpx2.Request) -> httpx2.Response:
    # A hostile or broken server that sends the key back in its error.
    echoed = request.headers.get("authorization", "")
    error = {"type": "authentication_error", "message": f"invalid credentials {echoed}"}
    return httpx2.Response(401, json={"type": "error", "error": error}, headers={"x": echoed})


def test_the_key_reaches_no_repr_log_span_or_error(
    keyed: Settings, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = Recorder(_echo_key)
    client = build_model_client(keyed, transport=recorder.transport())

    with provider.get_tracer("test").start_as_current_span("call") as span:
        with pytest.raises(ModelCallError) as failed:
            ask(client)
        span.record_exception(failed.value)
    error = failed.value

    # Control: the key was sent, as the header anthropic 2.3.0 sends it, so its absence
    # below means something.
    assert [r.headers.get("authorization") for r in recorder.requests] == [f"Bearer {key}"]
    assert (error.kind, error.status_code) == ("status", 401)
    texts = {
        "repr(settings)": repr(keyed),
        "str(settings)": str(keyed),
        "settings as JSON": keyed.model_dump_json(),
        "repr(client)": repr(client),
        "str(client)": str(client),
        "str(error)": str(error),
        "repr(error)": repr(error),
        "the formatted error": "".join(traceback.format_exception(error)),
        "the error's args": repr(error.args),
        "the log": caplog.text + "".join(repr(r.args) for r in caplog.records),
        "the span": "".join(s.to_json() for s in exporter.get_finished_spans()),
    }
    leaked = sorted(where for where, text in texts.items() if key in text)
    assert not leaked, f"the key leaked into: {leaked}"
    # Nothing of the SDK's exception is kept: not as cause, not as context.
    assert error.__cause__ is None and error.__context__ is None
    assert len(exporter.get_finished_spans()) == 1


def test_a_prompt_reaches_no_log_under_a_debug_root(
    keyed: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # The root logger at DEBUG: the SDK's loggers stay at INFO, so neither the request
    # options (the prompt) nor the response headers are logged.
    prompt = canary("prompt")
    caplog.set_level(logging.DEBUG)
    # The fixture below restored the levels: the factory, not an earlier test, holds them.
    assert logging.getLogger("anthropic").level == logging.NOTSET
    client = build_model_client(keyed, transport=Recorder().transport())
    ask(client, prompt)
    assert prompt not in caplog.text + "".join(repr(r.args) for r in caplog.records)
    # Control: the SDK's own logger at DEBUG does log the prompt, so the check above can
    # fail. The factory holds the level; it does not stop code that lowers it later.
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="anthropic")
    ask(client, prompt)
    assert prompt in caplog.text + "".join(repr(r.args) for r in caplog.records)


def test_a_quieter_sdk_logger_stays_quiet(keyed: Settings) -> None:
    logger = logging.getLogger("httpx2")
    before = logger.level
    try:
        logger.setLevel(logging.ERROR)
        build_model_client(keyed)
        assert logger.level == logging.ERROR
    finally:
        logger.setLevel(before)


# Review finding test-2: the loggers the SDK and its HTTP library write to, read from
# their source. Written out here, not taken from the module, so the module's list is
# checked against the libraries, not against itself.
HELD = ("anthropic", "httpx2", "httpcore2")


def library_loggers() -> set[str]:
    """Every logger name the three libraries create: getLogger("...") literals, and
    getLogger(__name__) as the module's own dotted name."""
    site = Path(anthropic.__file__).parent.parent
    literal = re.compile(r"""getLogger\(["']([A-Za-z0-9_.]+)["']\)""")
    names: set[str] = set()
    for top in HELD:
        for path in (site / top).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            names |= set(literal.findall(text))
            if "getLogger(__name__)" in text:
                module = path.relative_to(site).with_suffix("").as_posix().replace("/", ".")
                names.add(module.removesuffix(".__init__"))
    return names


def test_every_library_logger_is_held(keyed: Settings) -> None:
    names = library_loggers()
    # Non-vacuity: the ones that log a request or a response at DEBUG are found.
    assert {"anthropic._base_client", "httpcore2.http11", "httpx2"} <= names, sorted(names)
    assert {name.partition(".")[0] for name in names} == set(HELD)
    assert tuple(model_client.SDK_LOGGERS) == HELD
    logging.getLogger().setLevel(logging.DEBUG)
    build_model_client(keyed)
    low = sorted(n for n in names if logging.getLogger(n).getEffectiveLevel() < logging.INFO)
    assert not low, f"loggers below INFO: {low}"


def test_a_lowered_child_logger_is_raised(keyed: Settings) -> None:
    # Review finding sec-7: a child set below INFO before the build is held too.
    for name in ("anthropic._base_client", "httpcore2.http11"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    build_model_client(keyed)
    for name in ("anthropic._base_client", "httpcore2.http11"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.INFO, name


def test_the_sdk_makes_no_span(keyed: Settings, key: str) -> None:
    # Review finding test-1: the SDK traces through the global provider when it is given
    # none. The global one is replaced here by a recording one, for this test only.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Its own context: the conftest's monkeypatch outlives the provider check below.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(trace, "get_tracer_provider", lambda: provider)
        ask(build_model_client(keyed, transport=Recorder().transport()))
        assert exporter.get_finished_spans() == ()
        # Control: the SDK's defaults make one span per call through that provider.
        control = anthropic.Anthropic(
            api_key=key,
            max_retries=0,
            http_client=httpx2.Client(transport=Recorder().transport()),
        )
        control.messages.create(
            model=DEFAULT_MODEL, max_tokens=16, messages=[{"role": "user", "content": "Hello"}]
        )
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("text/plain", "not json {}"), ("application/json", "{{ {}"), ("application/json", "[]")],
)
def test_an_unreadable_answer_is_typed(keyed: Settings, content_type: str, body: str) -> None:
    # Review findings sec-2 and test-6: a 200 the SDK cannot read as a message.
    marker = canary("body")
    text = body.format(marker)
    recorder = Recorder(
        lambda request: httpx2.Response(200, content=text, headers={"content-type": content_type})
    )
    with pytest.raises(ModelCallError) as failed:
        ask(build_model_client(keyed, transport=recorder.transport()))
    error = failed.value
    assert (error.kind, error.status_code) == ("response", None)
    assert marker not in str(error) and marker not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize(
    "tool",
    [
        # A server tool: the API would fetch a URL itself (review finding sec-4).
        {"type": "web_fetch_20260209", "name": "web_fetch"},
        {"type": "web_search_20260209", "name": "web_search"},
        {"name": "x", "description": "x", "input_schema": {"type": "object"}, "type": "custom"},
        {"name": "x", "description": "x", "input_schema": {}, "cache_control": {"type": "x"}},
        {"name": "x", "input_schema": {"type": "object"}},
        "search_client_docs",
    ],
)
def test_only_registry_shaped_tools_are_sent(keyed: Settings, tool: Any) -> None:
    recorder = Recorder()
    client = build_model_client(keyed, transport=recorder.transport())
    with pytest.raises(model_client.RequestRefused):
        ask(client, tools=[*registry_tools(), tool])
    assert recorder.requests == []


# --- the request ----------------------------------------------------------------------


def registry_tools() -> list[dict[str, Any]]:
    registry = mvp_tools.build_registry()
    return [
        {
            "name": name,
            "description": registry.spec(name).description,
            "input_schema": registry.schema(name),
        }
        for name in registry.names()
    ]


def test_a_request_carries_exactly_the_registry_schemas(keyed: Settings, key: str) -> None:
    recorder = Recorder()
    tools = registry_tools()
    assert [tool["name"] for tool in tools] == list(mvp_tools.build_registry().names())
    answer = ask(build_model_client(keyed, transport=recorder.transport()), tools=tools)

    [request] = recorder.requests
    body = json.loads(request.content)
    assert (request.method, str(request.url)) == ("POST", MESSAGES_URL)
    assert set(body) == {"model", "max_tokens", "system", "messages", "tools"}, sorted(body)
    assert body["tools"] == tools
    assert "tool_choice" not in body
    assert body["model"] == keyed.model == DEFAULT_MODEL
    assert (body["system"], body["max_tokens"]) == (SYSTEM, 1024)
    # The key once, as anthropic 2.3.0 sends an API key: a bearer token. No second
    # credential header, and no beta header.
    assert request.headers.get_list("authorization") == [f"Bearer {key}"]
    assert "x-api-key" not in request.headers
    assert "anthropic-beta" not in request.headers
    assert answer == message_body()


def test_the_model_comes_from_settings(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    planted = "claude-planted-model"
    prices = dict(model_client.PRICES) | {planted: model_client.PRICES[DEFAULT_MODEL]}
    monkeypatch.setattr(model_client, "PRICES", prices)
    monkeypatch.setenv(MODEL, planted)
    recorder = Recorder(lambda request: httpx2.Response(200, json=message_body(planted)))
    ask(build_model_client(Settings(_env_file=None), transport=recorder.transport()))
    assert json.loads(recorder.requests[0].content)["model"] == planted


def test_a_refusal_is_an_answer(keyed: Settings) -> None:
    # stop_reason "refusal" comes with HTTP 200: the loop handles it, not the client.
    refusal = message_body(content=[], stop_reason="refusal")
    recorder = Recorder(lambda request: httpx2.Response(200, json=refusal))
    answer = ask(build_model_client(keyed, transport=recorder.transport()))
    assert answer["stop_reason"] == "refusal"


def test_no_trace_context_leaves_with_a_request(keyed: Settings, key: str) -> None:
    saved = propagate.get_global_textmap()
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    try:
        tracer = TracerProvider().get_tracer("test")
        ours, plain = Recorder(), Recorder()
        with tracer.start_as_current_span("ask"):
            ask(build_model_client(keyed, transport=ours.transport()))
            # Control: the SDK's defaults send the trace context of the current span.
            control = anthropic.Anthropic(
                api_key=key,
                max_retries=0,
                http_client=httpx2.Client(transport=plain.transport()),
            )
            control.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=16,
                messages=[{"role": "user", "content": "Hello"}],
            )
    finally:
        propagate.set_global_textmap(saved)
    assert "traceparent" in plain.requests[0].headers
    assert not {"traceparent", "tracestate", "baggage"} & set(ours.requests[0].headers)


def test_the_timeout_is_explicit(keyed: Settings) -> None:
    recorder = Recorder()
    ask(build_model_client(keyed, transport=recorder.transport()))
    timeout = recorder.requests[0].extensions["timeout"]
    total, connect = model_client.TIMEOUT_SECONDS, model_client.CONNECT_TIMEOUT_SECONDS
    assert timeout == {"connect": connect, "read": total, "write": total, "pool": total}


@pytest.mark.parametrize("status", [429, 500, 529])
def test_no_retries(keyed: Settings, status: int) -> None:
    recorder = Recorder(lambda request: httpx2.Response(status, json={"type": "error"}))
    with pytest.raises(ModelCallError) as failed:
        ask(build_model_client(keyed, transport=recorder.transport()))
    assert (failed.value.kind, failed.value.status_code) == ("status", status)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("raised", "kind"),
    [(httpx2.ReadTimeout, "timeout"), (httpx2.ConnectError, "connection")],
)
def test_transport_failures_are_typed_and_not_retried(
    keyed: Settings, raised: type[httpx2.TransportError], kind: str
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise raised(f"failed: {canary('detail')}", request=request)

    recorder = Recorder(fail)
    with pytest.raises(ModelCallError) as failed:
        ask(build_model_client(keyed, transport=recorder.transport()))
    assert (failed.value.kind, failed.value.status_code) == (kind, None)
    assert "canary" not in str(failed.value)
    assert len(recorder.requests) == 1


def test_redirects_are_not_followed(keyed: Settings) -> None:
    elsewhere = f"https://{branding.DOMAINS.attacker}/v1/messages"
    recorder = Recorder(lambda request: httpx2.Response(307, headers={"location": elsewhere}))
    with pytest.raises(ModelCallError) as failed:
        ask(build_model_client(keyed, transport=recorder.transport()))
    assert failed.value.status_code == 307
    assert [str(request.url) for request in recorder.requests] == [MESSAGES_URL]


# --- no proxy, no socket -------------------------------------------------------------


@pytest.fixture
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """getaddrinfo answers for loopback and refuses every other name, recording it."""
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host in ("127.0.0.1", b"127.0.0.1"):
            return real(host, *args, **kwargs)
        looked_up.append(host.decode() if isinstance(host, bytes) else str(host))
        raise socket.gaierror(socket.EAI_NONAME, "stubbed: no lookups in tests")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return looked_up


def _pending(listener: socket.socket) -> int:
    count = 0
    while True:
        try:
            connection, _ = listener.accept()
        except BlockingIOError:
            return count
        connection.close()
        count += 1


def test_proxy_variables_are_not_used(
    monkeypatch: pytest.MonkeyPatch, keyed: Settings, stub_dns: list[str]
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.setblocking(False)
        proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"
        for name in PROXY_VARIABLES:
            monkeypatch.setenv(name, proxy)
        monkeypatch.delenv("NO_PROXY")
        monkeypatch.delenv("no_proxy")

        with pytest.raises(ModelCallError) as failed:
            ask(build_model_client(keyed))
        assert failed.value.kind == "connection"
        # The client went for the API host itself, and nothing reached the proxy.
        assert stub_dns == ["api.anthropic.com"], stub_dns
        assert _pending(listener) == 0

        # Control: a client that trusts the environment goes to the proxy.
        with httpx2.Client(timeout=0.5) as trusting, pytest.raises(httpx2.HTTPError):
            trusting.post(MESSAGES_URL)
        assert _pending(listener) == 1


def test_no_socket_opens(monkeypatch: pytest.MonkeyPatch, keyed: Settings) -> None:
    opened: list[str] = []

    def refuse(what: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            opened.append(what)
            raise AssertionError(f"{what} during an offline call")

        return call

    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    build_model_client(keyed)  # the real transport: built, never used
    ask(build_model_client(keyed, transport=Recorder().transport()))
    assert opened == []


# --- cost -------------------------------------------------------------------------------

# Anthropic's pricing page, read on 2026-11-09, in USD per million tokens. Written out
# here, not taken from the module, so the table is checked against the page's numbers.
PAGE = {
    "claude-sonnet-5-5": {
        "input": "2",
        "cache_write_5m": "2.50",
        "cache_write_1h": "4",
        "cache_read": "0.10",
        "output": "10",
    }
}


def test_the_price_table_is_the_page() -> None:
    assert model_client.PRICES_AS_OF == "2026-11-09"
    table = {
        model: {field: getattr(price, field) for field in PAGE[DEFAULT_MODEL]}
        for model, price in model_client.PRICES.items()
    }
    assert table == {
        model: {field: Decimal(value) for field, value in fields.items()}
        for model, fields in PAGE.items()
    }
    assert DEFAULT_MODEL in model_client.PRICES
    assert dict(model_client.GEO_MULTIPLIERS) == {"global": Decimal(1), "us": Decimal("1.1")}


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "12"),
        ({"input_tokens": 0, "output_tokens": 0}, "0"),
        ({}, "0"),
        # The breakdown of cache writes by duration.
        (
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 3000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1000,
                    "ephemeral_1h_input_tokens": 2000,
                },
                "output_tokens": 500,
            },
            "0.0177",
        ),
        # Without the breakdown, every cache write is priced as a 5-minute one.
        (
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 3000,
                "output_tokens": 500,
            },
            "0.0147",
        ),
        ({"input_tokens": 1_000_000, "inference_geo": "global"}, "2"),
        ({"input_tokens": 1_000_000, "output_tokens": 100_000, "inference_geo": "us"}, "3.3"),
        (
            {"input_tokens": 3, "cache_creation_input_tokens": None, "cache_creation": None},
            "0.000006",
        ),
        # The standard tier and speed, and no server tool used, are what the table prices.
        (
            {
                "input_tokens": 1_000_000,
                "service_tier": "standard",
                "speed": "standard",
                "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            },
            "2",
        ),
    ],
)
def test_cost_is_usage_times_the_table(usage: dict[str, Any], expected: str) -> None:
    cost = cost_usd(DEFAULT_MODEL, usage)
    assert isinstance(cost, Decimal)
    assert cost == Decimal(expected), cost


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 1, "inference_geo": "eu"},
        {"input_tokens": -1},
        {"input_tokens": 1.5},
        {"input_tokens": True},
        {"input_tokens": "1"},
        # Review findings sec-6 and test-11: tiers, speeds and server tools the table does
        # not price.
        {"input_tokens": 1, "service_tier": "priority"},
        {"input_tokens": 1, "service_tier": "batch"},
        {"input_tokens": 1, "speed": "fast"},
        {"input_tokens": 1, "server_tool_use": {"web_search_requests": 1}},
        {"input_tokens": 1, "server_tool_use": {"web_fetch_requests": 2, "web_search_requests": 0}},
        # A breakdown that does not add up to the total.
        {
            "cache_creation_input_tokens": 3,
            "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 1},
        },
    ],
)
def test_cost_refuses_what_the_table_cannot_price(usage: dict[str, Any]) -> None:
    with pytest.raises(PriceUnknown):
        cost_usd(DEFAULT_MODEL, usage)


def test_cost_refuses_a_model_without_a_price() -> None:
    with pytest.raises(PriceUnknown):
        cost_usd("claude-without-a-price", {"input_tokens": 1})


def test_cost_of_a_real_answer(keyed: Settings) -> None:
    usage = {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0}
    recorder = Recorder(lambda request: httpx2.Response(200, json=message_body(usage=usage)))
    answer = ask(build_model_client(keyed, transport=recorder.transport()))
    assert cost_usd(DEFAULT_MODEL, answer["usage"]) == Decimal("0.0054")


@pytest.fixture(autouse=True)
def _restore_library_loggers() -> Iterator[None]:
    # Review finding test-3: the factory sets levels on process-wide loggers; each test
    # here starts from, and leaves, the levels it found. The root's level too.
    names = [""] + sorted(library_loggers())
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def _no_global_tracer_provider() -> Iterator[None]:
    # The app never sets the global tracer provider; a test here must not either.
    before = trace.get_tracer_provider()
    yield
    assert trace.get_tracer_provider() is before
