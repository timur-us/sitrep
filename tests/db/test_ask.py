"""POST /api/ask through the real READ handlers (M1 plan, PR 12; threat-model paths 2 and 8).

In process, through TestClient with the lifespan (so the app's own pool opens), the dev
login's middleware on a 127.0.0.1 peer, the loaded corpus, the real embedder, and a
scripted model client (tests/scripted_model.py), which searches and then cites what the
search returned. No socket but loopback, no key.

- The slice: the demo advisor asks about the demo family and gets an answer that cites
  their own documents, with title, kind and trust from the database, grounding counts,
  no proposals, the usage, the cost as an exact decimal string, the stop and the trace
  id. Every e-mail of the demo mandate is cited as untrusted, every other record of it
  as internal (Timur's answer 2 on #35).
- The boundary (PR 7's cross-advisor handover, at the API level): the other advisors,
  asking about the demo family, get no record id of the demo advisor's, neither in the
  response nor in anything the model was sent.
- No connection is held while the model is called: every call sees the pool with all
  its connections free.
- The rate limit per advisor: over it, the demo advisor gets 429 while another does not;
  one request in flight per advisor.
- The route's deadline (Timur's answer 1 on #33): no model call starts after it, and
  the request ends with stop "budget".
- The handovers: a refused query reaches the model by its kind; a GuardError from a
  handler ends the request (500), a database that stops answering is 503; each
  LoopError kind has its status and one fixed body.
- No text in spans: a canary in the question, the corpus and the answer reaches no
  span, while the request's span carries the loop's attributes.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import anyio
import psycopg
import pytest
from dbtools import SUPERUSER_URL
from fastapi.testclient import TestClient
from loaded_corpus import Tables, ensure_loaded, read_tables, table
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql
from psycopg_pool import PoolTimeout
from scripted_model import SEARCH, USAGE, EchoModel, reply, text, tool_results, tool_use

from app.api import ask
from app.auth import dev_shim
from app.config import branding
from app.config.settings import DEFAULT_MODEL, LOOPBACK, Settings, env_name
from app.db import load
from app.db.connect import connect
from app.db.session import GuardError
from app.embeddings.onnx import OnnxEmbedder
from app.harness import handlers as read_tools
from app.harness.context import Provenance, Untrusted
from app.harness.model_client import ModelCallError, cost_usd
from app.harness.mvp_tools import DocumentHit
from app.main import create_app

pytestmark = pytest.mark.usefixtures("database")

PORT = 8000
PORTFOLIO = read_tools.PORTFOLIO
BASE = f"http://{LOOPBACK}:{PORT}"
ORIGIN = {"Origin": BASE}
JSON = {**ORIGIN, "Content-Type": "application/json"}
PEER = (LOOPBACK, 50000)
DEMO = branding.DEMO_MANDATE
MINE = DEMO.advisor_id
OTHERS = [person.id for person in branding.ADVISORS if person.id != MINE]
OTHER = OTHERS[0]
QUESTIONS = {
    "en": f"What do I need to know before the call with {DEMO.family}?",
    "de": f"Was muss ich vor dem Gespräch mit Familie {DEMO.family} wissen?",
}
CITATION = {"record_id", "title", "kind", "trust"}
CONTRACT = {
    "answer",
    "citations",
    "grounding",
    "proposals",
    "usage",
    "cost_usd",
    "stop",
    "trace_id",
}


class Clock:
    def __init__(self) -> None:
        self.now = 5_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def corpus(
    database: None, embedder: OnnxEmbedder, tmp_path_factory: pytest.TempPathFactory
) -> load.Corpus:
    return ensure_loaded(embedder, tmp_path_factory.mktemp("corpus"))


@pytest.fixture(scope="module")
def tables(corpus: load.Corpus) -> Tables:
    return read_tables()


@pytest.fixture
def dev_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(env_name("dev_auth"), "1")
    monkeypatch.setenv(env_name("port"), str(PORT))
    return Settings(_env_file=None)


class App:
    """A started app (lifespan run, pool open) and its client; logins by cookie."""

    def __init__(self, test_client: TestClient) -> None:
        self.client = test_client
        self.cookies: dict[str, str] = {}

    @property
    def app(self) -> Any:
        return self.client.app

    def login(self, advisor_id: str) -> None:
        self.client.cookies.clear()
        response = self.client.post(
            "/dev/login",
            content=f"advisor={advisor_id}",
            headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200, f"login as {advisor_id}: {response.status_code}"
        self.cookies[advisor_id] = response.cookies[dev_shim.COOKIE]
        self.client.cookies.clear()

    def ask(self, advisor_id: str, question: str = QUESTIONS["en"], **body: Any) -> Any:
        if advisor_id not in self.cookies:
            self.login(advisor_id)
        headers = {**JSON, "Cookie": f"{dev_shim.COOKIE}={self.cookies[advisor_id]}"}
        return self.client.post(ask.ROUTE, json={"question": question, **body}, headers=headers)


@pytest.fixture
def start(dev_settings: Settings, embedder: OnnxEmbedder, corpus: load.Corpus) -> Iterator[Any]:
    """start(model, **create_app kwargs) -> App; every app is stopped after the test."""
    stack: list[TestClient] = []

    def started(model: object, **kwargs: Any) -> App:
        app = create_app(
            dev_settings, bind_host=LOOPBACK, model_client=model, embedder=embedder, **kwargs
        )
        test_client = TestClient(app, base_url=BASE, client=PEER, raise_server_exceptions=False)
        test_client.__enter__()
        stack.append(test_client)
        return App(test_client)

    yield started
    for test_client in reversed(stack):
        test_client.__exit__(None, None, None)


def own_or_sheet(tables: Tables, advisor_id: str, record_id: str) -> bool:
    return tables.owner.get(record_id) == advisor_id or record_id in tables.sheets


def demo_records(tables: Tables) -> set[str]:
    return {record for record, owner in tables.owner.items() if owner == MINE}


def assert_json_no_store(response: Any) -> None:
    assert response.headers.get("content-type") == "application/json", response.headers
    assert response.headers.get("cache-control") == "no-store", response.headers


# The slice.


@pytest.mark.parametrize("lang", QUESTIONS)
def test_the_demo_advisor_gets_a_cited_answer_from_own_documents(
    start: Any, tables: Tables, lang: str
) -> None:
    model = EchoModel(DEMO.family)
    response = start(model).ask(MINE, QUESTIONS[lang])
    assert response.status_code == 200, response.text
    assert_json_no_store(response)
    body = response.json()
    assert set(body) == CONTRACT, sorted(body)
    assert body["stop"] == "end_turn"
    assert body["proposals"] == []
    citations = body["citations"]
    assert citations, "no citation"
    assert all(set(item) == CITATION for item in citations), citations
    for item in citations:
        record = item["record_id"]
        assert own_or_sheet(tables, MINE, record), f"{record} is not the demo advisor's"
        assert item["title"] == tables.title[record]
        kind = read_tools.PRODUCT_SHEET if record in tables.sheets else read_tools.DOCUMENT
        assert item["kind"] == kind
        assert item["trust"] == tables.trust[record]
        assert f"[source: {record}]" in body["answer"]
    assert any(tables.owner.get(item["record_id"]) == MINE for item in citations)
    assert body["grounding"] == {"figures": 0, "unmatched": 0, "unknown_citations": 0}
    assert body["usage"] == {
        "input_tokens": 2 * USAGE["input_tokens"],
        "output_tokens": 2 * USAGE["output_tokens"],
    }
    expected = 2 * cost_usd(DEFAULT_MODEL, USAGE)
    assert isinstance(body["cost_usd"], str) and Decimal(body["cost_usd"]) == expected
    assert "e" not in body["cost_usd"].lower(), body["cost_usd"]
    assert body["trace_id"] == response.headers["x-trace-id"]


def test_a_cited_product_sheet_is_named_as_one(start: Any, tables: Tables) -> None:
    sheet = min(tables.sheets)
    model = EchoModel(tables.title[sheet], cite=20)
    body = start(model).ask(MINE, "What does the fund sheet say?").json()
    kinds = {item["record_id"]: item["kind"] for item in body["citations"]}
    assert kinds.get(sheet) == read_tools.PRODUCT_SHEET, kinds
    trust = {item["record_id"]: item["trust"] for item in body["citations"]}
    assert trust.get(sheet) == tables.trust[sheet] == "internal", trust


def test_the_demo_mandates_e_mails_are_cited_as_untrusted(start: Any, tables: Tables) -> None:
    # Timur's answer 2 on #35: a mailbox record's title is its sender's subject line, so
    # the citation says it is untrusted. The search keeps to the demo mandate, whose few
    # records all fit in one search, and every record it returns is cited.
    own = demo_records(tables)
    model = EchoModel(DEMO.family, cite=20, arguments={"mandate_id": DEMO.id, "limit": 20})
    response = start(model).ask(MINE, QUESTIONS["en"], mandate_id=DEMO.id)
    assert response.status_code == 200, response.text
    trust = {item["record_id"]: item["trust"] for item in response.json()["citations"]}
    assert set(trust) <= own, sorted(set(trust) - own)
    mail = {record for record in trust if tables.origin[record] == "mailbox"}
    assert mail, "control: no e-mail of the demo mandate was cited"
    assert set(trust) - mail, "control: no other record of the demo mandate was cited"
    assert {record: trust[record] for record in mail} == dict.fromkeys(mail, "untrusted")
    others = set(trust) - mail
    assert {record: trust[record] for record in others} == dict.fromkeys(others, "internal")
    assert trust == {record: tables.trust[record] for record in trust}


# The boundary.


@pytest.mark.parametrize("lang", QUESTIONS)
@pytest.mark.parametrize("advisor_id", OTHERS)
def test_another_advisor_asking_about_the_demo_family_gets_nothing_of_theirs(
    start: Any, tables: Tables, advisor_id: str, lang: str
) -> None:
    model = EchoModel(DEMO.family, cite=20)
    response = start(model).ask(advisor_id, QUESTIONS[lang])
    assert response.status_code == 200, response.text
    body = response.json()
    citations = [item["record_id"] for item in body["citations"]]
    assert citations, "control: the search returned nothing to cite"
    assert all(own_or_sheet(tables, advisor_id, record) for record in citations), citations
    leaked = sorted(record for record in demo_records(tables) if record in response.text)
    assert leaked == [], f"the demo advisor's record ids in {advisor_id}'s response: {leaked[:3]}"
    seen = model.seen()
    leaked = sorted(record for record in demo_records(tables) if record in seen)
    assert leaked == [], (
        f"the demo advisor's record ids sent to the model for {advisor_id}: {leaked[:3]}"
    )


def test_control_the_demo_advisors_ids_are_found_for_them(start: Any, tables: Tables) -> None:
    # The boundary test's control: the same search as the demo advisor does reach her records.
    model = EchoModel(DEMO.family, cite=20)
    response = start(model).ask(MINE)
    assert any(record in response.text for record in demo_records(tables))


def test_a_mandate_of_another_advisor_finds_nothing(start: Any, tables: Tables) -> None:
    # The mandate the advisor names goes to the model as text; the handlers look it up
    # under row-level security, so the demo mandate named by another advisor is no mandate.
    model = EchoModel(DEMO.family, cite=20)
    response = start(model).ask(OTHER, mandate_id=DEMO.id)
    assert response.status_code == 200, response.text
    assert not any(record in response.text for record in demo_records(tables))
    assert model.calls[0]["messages"][0]["content"].endswith(f"Mandate in focus: {DEMO.id}")


# No connection held during a model call.


def test_the_pool_is_free_during_every_model_call(start: Any) -> None:
    seen: list[dict[str, int]] = []
    holder: list[App] = []

    def during(model: EchoModel) -> None:
        seen.append(dict(holder[0].app.state.pool.get_stats()))

    model = EchoModel(DEMO.family, on_call=during)
    holder.append(start(model))
    assert holder[0].ask(MINE).status_code == 200
    assert len(seen) == 2, seen
    for stats in seen:
        size = stats.get("pool_size", 0)
        assert size >= 1, f"control: the pool has no connection yet: {stats}"
        assert stats.get("pool_available", 0) == size, f"a connection is out: {stats}"
        assert stats.get("requests_waiting", 0) == 0, stats


# The rate limit.


def test_over_the_limit_one_advisor_gets_429_while_another_does_not(start: Any) -> None:
    clock = Clock()
    app = start(EchoModel(DEMO.family), clock=clock)
    for _ in range(ask.REQUESTS_PER_WINDOW):
        assert app.ask(MINE).status_code == 200
    refused = app.ask(MINE)
    assert refused.status_code == 429, refused.status_code
    assert refused.json() == {"detail": ask.TOO_MANY}
    assert_json_no_store(refused)
    assert app.ask(OTHER).status_code == 200, (
        "the other advisor is limited by the other advisor's requests"
    )
    clock.now += ask.WINDOW_SECONDS
    assert app.ask(MINE).status_code == 200, "the window did not slide"


def test_one_request_in_flight_per_advisor(start: Any) -> None:
    # During the demo advisor's model call, a second ask of hers is 429 and one of the other advisor's is not.
    nested: dict[str, int] = {}
    holder: list[App] = []

    def during(model: EchoModel) -> None:
        if nested:
            return
        nested["mine"] = 0  # mark: the nested asks call the model too
        nested["mine"] = holder[0].ask(MINE).status_code
        nested["other"] = holder[0].ask(OTHER).status_code

    holder.append(start(EchoModel(DEMO.family, on_call=during)))
    holder[0].login(OTHER)
    assert holder[0].ask(MINE).status_code == 200
    assert nested == {"mine": 429, "other": 200}, nested
    assert holder[0].ask(MINE).status_code == 200, "the slot was not released"


# The deadline.


def test_no_model_call_starts_after_the_deadline(start: Any) -> None:
    clock = Clock()

    def late(model: EchoModel) -> None:
        clock.now += ask.DEADLINE_SECONDS  # the first call took the whole time

    model = EchoModel(DEMO.family, on_call=late)
    response = start(model, clock=clock).ask(MINE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["stop"], body["answer"], body["citations"]) == ("budget", "", [])
    assert len(model.calls) == 1, "a model call started after the deadline"
    assert body["usage"]["input_tokens"] == USAGE["input_tokens"]


def test_control_just_before_the_deadline_the_call_runs(start: Any) -> None:
    clock = Clock()

    def nearly(model: EchoModel) -> None:
        if len(model.calls) == 1:
            clock.now += ask.DEADLINE_SECONDS - 0.5

    model = EchoModel(DEMO.family, on_call=nearly)
    body = start(model, clock=clock).ask(MINE).json()
    assert body["stop"] == "end_turn" and len(model.calls) == 2


def test_the_deadline_counts_from_the_requests_start(
    start: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding spec-7: time spent before the route function (here the first
    # ask's model client build) counts against the deadline too.
    clock = Clock()
    model = EchoModel(DEMO.family)

    def slow_build(settings: Settings) -> EchoModel:
        clock.now += ask.DEADLINE_SECONDS
        return model

    monkeypatch.setattr(ask, "build_model_client", slow_build)
    response = start(None, clock=clock).ask(MINE)
    assert response.status_code == 200, response.text
    assert (response.json()["stop"], response.json()["answer"]) == ("budget", "")
    assert model.calls == [], "a model call started after the deadline"


def test_the_slot_is_free_before_the_response_is_sent(start: Any) -> None:
    # Review finding test-8: the in-flight slot is released when the route returns
    # (scope "function"), before the response goes out; with the request scope a quick
    # second ask would meet a 429. Driven over raw ASGI, so the send is observed.
    app = start(EchoModel(DEMO.family))
    app.login(MINE)
    limit = app.app.state.ask.limit
    body = json.dumps({"question": QUESTIONS["en"]}).encode()
    headers = [
        (b"host", f"{LOOPBACK}:{PORT}".encode()),
        (b"origin", BASE.encode()),
        (b"content-type", b"application/json"),
        (b"cookie", f"{dev_shim.COOKIE}={app.cookies[MINE]}".encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": ask.ROUTE,
        "raw_path": ask.ROUTE.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": PEER,
        "server": (LOOPBACK, PORT),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    seen: list[tuple[int, bool]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen.append((message["status"], MINE in limit._busy))

    anyio.run(app.app, scope, receive, send)
    assert seen == [(200, False)], seen


# The handovers.


def test_a_citation_of_a_record_no_tool_returned_is_not_listed(start: Any, tables: Tables) -> None:
    # Review finding test-1: a model (an injected one, say) cites another advisor's
    # record id that no tool returned in this request. It is counted as unknown and
    # never looked up, so neither the id's title nor a citation reaches the answer.
    own = start(EchoModel(DEMO.family)).ask(MINE).json()
    record = next(
        c["record_id"] for c in own["citations"] if tables.owner.get(c["record_id"]) == MINE
    )
    assert record in [c["record_id"] for c in own["citations"]], "control: listed for its owner"
    search = tool_use("tu_1", SEARCH, {"query": DEMO.family})
    forged = EchoModel(
        DEMO.family,
        script=[reply(search, stop="tool_use"), reply(text(f"See [source: {record}]."))],
    )
    response = start(forged).ask(OTHER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["citations"] == []
    assert body["grounding"]["unknown_citations"] == 1
    assert tables.title[record] not in response.text
    assert record not in str(forged.calls[1]["messages"]), "the search returned the record"


def test_the_grounding_counts_are_the_reports(start: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Review finding test-2: distinct non-zero counts, so no field can stand in for
    # another. One figure is in the source, one is not; three cited ids are unknown.
    def search(advisor: object, arguments: object) -> Untrusted[Any]:
        hit = DocumentHit(
            kind="document",
            document_id="doc-figures",
            mandate_id=DEMO.id,
            title="Report",
            lang="english",
            trust="internal",
            excerpt="Total assets EUR 1,250,000.00.",
        )
        return Untrusted([hit], Provenance(tool=SEARCH, record_ids=("doc-figures",)))

    monkeypatch.setattr(
        ask, "read_handlers", lambda pool, embedder: {SEARCH: search, PORTFOLIO: search}
    )
    answer = (
        "Assets EUR 1,250,000.00, equity 42.5%. "
        "[source: doc-figures] [source: x-1] [source: x-2] [source: x-3]"
    )
    search_call = tool_use("tu_1", SEARCH, {"query": DEMO.family})
    model = EchoModel(
        DEMO.family, script=[reply(search_call, stop="tool_use"), reply(text(answer))]
    )
    body = start(model).ask(MINE).json()
    assert body["grounding"] == {"figures": 2, "unmatched": 1, "unknown_citations": 3}
    # doc-figures was returned by the tool but is no row of the database: left out.
    assert body["citations"] == []


def test_a_query_too_long_reaches_the_model_by_its_kind(start: Any) -> None:
    # PR 8's measured over-limit query: under the schema's 500 characters, over the
    # model's 512 tokens. The handler refuses it as ToolFailed(query_too_long).
    model = EchoModel("ﷺ" * 130)
    response = start(model).ask(MINE)
    assert response.status_code == 200, response.text
    [block] = tool_results(model.calls[1]["messages"])
    assert block["is_error"] is True
    assert read_tools.QUERY_TOO_LONG in block["content"], block["content"]


def failing_handlers(error: Exception) -> Any:
    def handler(advisor: object, arguments: object) -> Untrusted[Any]:
        raise error

    def build(pool: object, embedder: object) -> dict[str, Any]:
        return {read_tools.SEARCH: handler, read_tools.PORTFOLIO: handler}

    return build


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (GuardError("a row of another advisor reached a READ handler"), 500, ask.FAILED),
        (PoolTimeout("couldn't get a connection after 5.00 sec"), 503, ask.NO_DATABASE),
        (psycopg.OperationalError("server closed the connection"), 503, ask.NO_DATABASE),
    ],
    ids=["guard", "pool-timeout", "operational"],
)
def test_a_guard_or_database_failure_in_a_handler_ends_the_request(
    start: Any, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, detail: str
) -> None:
    monkeypatch.setattr(ask, "read_handlers", failing_handlers(error))
    model = EchoModel(DEMO.family)
    response = start(model).ask(MINE)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert_json_no_store(response)
    assert len(model.calls) == 1, "the model was asked again after the failure"


CANARY_TEXT = "cnry-ask-3b9e"
SERVER_TOOL = {"type": "server_tool_use", "id": "st_1", "name": "web_fetch", "input": {}}
PRIORITY = reply(text("x")) | {"usage": USAGE | {"service_tier": "priority"}}
LOOP_ERRORS = {
    "model_call": ([ModelCallError("status", 400)], 502, ask.MODEL_FAILED),
    "protocol": ([reply(text("x"), model="claude-opus-5-5")], 500, ask.FAILED),
    "unexpected_stop": ([reply(text("x"), stop="pause_turn")], 500, ask.FAILED),
    "message_refused": ([reply(SERVER_TOOL)], 500, ask.FAILED),
    "price_unknown": ([PRIORITY], 500, ask.FAILED),
    "internal": ([RuntimeError(CANARY_TEXT)], 500, ask.FAILED),
}


def unfiltered() -> tuple[TracerProvider, InMemorySpanExporter]:
    """A provider that exports spans as they are: the route must put no text there."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def ask_span(exporter: InMemorySpanExporter) -> dict[str, Any]:
    [server] = [s for s in exporter.get_finished_spans() if s.name == f"POST {ask.ROUTE}"]
    return dict(server.attributes or {})


ERROR = f"{branding.SLUG}.loop.error"
STOP = f"{branding.SLUG}.loop.stop"


@pytest.mark.parametrize(
    ("kind", "script", "status", "detail"),
    [(kind, *case) for kind, case in LOOP_ERRORS.items()],
    ids=LOOP_ERRORS,
)
def test_each_loop_error_has_its_status_and_one_fixed_body(
    start: Any, kind: str, script: list[object], status: int, detail: str
) -> None:
    provider, exporter = unfiltered()
    model = EchoModel(DEMO.family, script=script)
    response = start(model, tracer_provider=provider).ask(MINE, f"About {CANARY_TEXT}")
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert_json_no_store(response)
    assert response.headers.get("x-trace-id"), "no trace id to report"
    assert CANARY_TEXT not in response.text
    # Review finding sec-9: no text on the failure paths' spans either.
    dumped = "\n".join(span.to_json() for span in exporter.get_finished_spans())
    assert CANARY_TEXT not in dumped, "the canary reached a span"
    assert CANARY_TEXT in model.seen(), "control: the question reached the model"
    assert ask_span(exporter)[ERROR] == kind


def test_a_handler_result_outside_the_contract_is_500(
    start: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review finding test-13: the "handler" kind through the route.
    def plain(advisor: object, arguments: object) -> str:
        return "EUR 5"

    monkeypatch.setattr(
        ask, "read_handlers", lambda pool, embedder: {SEARCH: plain, PORTFOLIO: plain}
    )
    response = start(EchoModel(DEMO.family)).ask(MINE)
    assert (response.status_code, response.json()) == (500, {"detail": ask.FAILED})


@pytest.mark.parametrize(
    ("error", "status", "detail", "kind"),
    [
        (GuardError("a row of another advisor reached a READ handler"), 500, ask.FAILED, "guard"),
        (PoolTimeout("couldn't get a connection after 5.00 sec"), 503, ask.NO_DATABASE, "database"),
    ],
    ids=["guard", "database"],
)
def test_a_failure_looking_up_the_citations_ends_the_request(
    start: Any,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    detail: str,
    kind: str,
) -> None:
    # Review findings test-3 and sec-1: after the loop, the citations' lookup refuses a
    # row of another advisor (a broken boundary) or loses the database; the request
    # ends with the fixed body, and the span names the kind for M2's alarm.
    def failing(pool: object, advisor: object, record_ids: object) -> Any:
        raise error

    monkeypatch.setattr(ask, "cited_records", failing)
    provider, exporter = unfiltered()
    model = EchoModel(DEMO.family)
    response = start(model, tracer_provider=provider).ask(MINE)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    assert_json_no_store(response)
    assert len(model.calls) == 2, "control: the loop ran to its answer"
    attributes = ask_span(exporter)
    assert (attributes[STOP], attributes.get(ERROR)) == ("end_turn", kind)


@pytest.mark.parametrize(
    ("error", "status", "detail", "kind"),
    [
        (GuardError("the connection is not idle"), 500, ask.FAILED, "guard"),
        (PoolTimeout("couldn't get a connection after 5.00 sec"), 503, ask.NO_DATABASE, "database"),
    ],
    ids=["guard", "database"],
)
def test_a_failure_before_the_first_model_call_ends_the_request(
    start: Any,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    detail: str,
    kind: str,
) -> None:
    # Review findings test-3 and sec-1: the check before the first model call.
    @contextmanager
    def failing(pool: object, advisor: object) -> Iterator[Any]:
        raise error
        yield  # a generator, as the real one

    monkeypatch.setattr(ask, "advisor_transaction", failing)
    provider, exporter = unfiltered()
    model = EchoModel(DEMO.family)
    response = start(model, tracer_provider=provider).ask(MINE)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    assert model.calls == [], "the model was called before the check"
    assert ask_span(exporter).get(ERROR) == kind


def test_every_loop_error_kind_is_mapped() -> None:
    kinds = {"model_call", "message_refused", "unexpected_stop", "protocol", "handler"}
    kinds |= {"price_unknown", "internal", "guard", "database"}
    assert set(ask.STATUS) == kinds
    assert {kind: ask.STATUS[kind][0] for kind in ("model_call", "database", "guard")} == {
        "model_call": 502,
        "database": 503,
        "guard": 500,
    }


# No text in spans.


@pytest.fixture
def canary_document() -> Iterator[tuple[str, str]]:
    """One of the demo advisor's documents, in the demo mandate, whose title and body carry a canary."""
    word = "cnry" + secrets.token_hex(5)
    record = f"pr12-canary-{word}"
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        columns = ("advisor_id", "id", "mandate_id", "title", "body", "lang", "origin")
        values = (
            MINE,
            record,
            DEMO.id,
            f"Note {word}",
            f"The client mentioned {word}.",
            "english",
            "call_notes",
        )
        conn.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                table("documents"),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join([sql.Placeholder()] * len(columns)),
            ),
            values,
        )
        try:
            yield word, record
        finally:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(table("documents")), (record,)
            )


def test_no_text_reaches_a_span(start: Any, canary_document: tuple[str, str]) -> None:
    word, record = canary_document
    exporter = InMemorySpanExporter()
    provider = TracerProvider()  # unfiltered: the route itself must put no text there
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    model = EchoModel(word, answer=f"About {word}:")
    response = start(model, tracer_provider=provider).ask(MINE, f"Tell me about {word}")
    assert response.status_code == 200, response.text
    body = response.json()
    # Control: the canary went through the tool result, the answer and the citations.
    assert word in body["answer"] and record in [c["record_id"] for c in body["citations"]]
    assert word in model.seen()
    spans = exporter.get_finished_spans()
    dumped = "\n".join(span.to_json() for span in spans)
    assert word not in dumped, "the canary reached a span"
    assert DEMO.family not in dumped
    [server] = [span for span in spans if span.name == f"POST {ask.ROUTE}"]
    attributes = dict(server.attributes or {})
    # The request's span is current in the thread that runs the loop (PR 10 handover).
    assert attributes[f"{branding.SLUG}.loop.stop"] == "end_turn", attributes
    assert attributes[f"{branding.SLUG}.loop.tools"] == ("search_client_docs",)
    assert format(server.context.trace_id, "032x") == body["trace_id"]


def test_a_failure_is_traced_by_kind(start: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ask, "read_handlers", failing_handlers(GuardError("foreign row")))
    response = start(EchoModel(DEMO.family), tracer_provider=provider).ask(MINE)
    assert response.status_code == 500
    [server] = [s for s in exporter.get_finished_spans() if s.name == f"POST {ask.ROUTE}"]
    assert dict(server.attributes or {})[f"{branding.SLUG}.loop.error"] == "guard"


def test_the_response_passes_the_pages_reading_of_the_contract(start: Any) -> None:
    # #34 (PR 11), question 1: the page accepts a response only if trace_id is 32
    # lower-case hex digits, stop is one of the four, every count is a non-negative
    # integer with unmatched <= figures, and cost_usd is a non-negative JSON number or a
    # plain decimal string without an exponent; and (Timur's answer 2 on #35) every
    # citation's trust is exactly "internal" or "untrusted". The real route's response
    # passes.
    body = start(EchoModel(DEMO.family, cite=20, arguments={"limit": 20})).ask(MINE).json()
    assert body["citations"], "control: nothing cited"
    for item in body["citations"]:
        assert set(item) == CITATION and all(type(value) is str for value in item.values())
        assert item["trust"] in {"internal", "untrusted"}, item["trust"]
    assert re.fullmatch(r"[0-9a-f]{32}", body["trace_id"]), body["trace_id"]
    assert body["stop"] in {"end_turn", "refusal", "budget", "max_turns"}
    counts = [*body["grounding"].values(), *body["usage"].values()]
    assert all(type(count) is int and count >= 0 for count in counts), counts
    assert body["grounding"]["unmatched"] <= body["grounding"]["figures"]
    assert re.fullmatch(r"[0-9]+(\.[0-9]+)?", body["cost_usd"]), body["cost_usd"]
