"""The agent loop with the two READ tools (M1 plan, PR 10; ADR 0001).

A scripted model client stands in for the API and test-local handlers for the database,
so nothing here opens a socket or needs a key. The loop:
- handles every stop reason, and ends with a stated stop, never a silent truncation;
- puts the tool_result blocks of one turn, and only them, in one user message, in the
  order of the tool_use blocks;
- offers the two READ tools only, with the registry's schemas and no tool_choice;
- renders every tool result through wrap();
- checks every message block against an allowlist before each call;
- charges the token budget after every call and every wrap(), and a failed call its
  worst case;
- puts only approved, text-free attributes on the span.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import traceback
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from app.auth.identity import VerifiedAdvisor, issue
from app.config import branding
from app.harness import loop, prompt
from app.harness.context import Provenance, Untrusted
from app.harness.grounding import CITATION_FORM
from app.harness.model_client import PRICES, ModelCallError, cost_usd
from app.harness.mvp_tools import DocumentHit, Portfolio
from app.observability import tracing

MODEL = "claude-sonnet-5-5"
assert MODEL in PRICES


def key(name: str) -> str:
    """A loop attribute name, under the product slug (app/config/branding.py)."""
    return f"{branding.SLUG}.{name}"


ADVISOR = branding.DEMO_MANDATE.advisor_id
MANDATE = branding.DEMO_MANDATE.id
CANARY = "cnry-loop-5e1b"


# --- The scripted model --------------------------------------------------------------


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value, "citations": None}


def tool_use(block_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def thinking(value: str = "", signature: str = "sig-1") -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": signature}


def response(*content: dict[str, Any], stop: str | None = "end_turn", **usage: int) -> dict:
    counts = {"input_tokens": 1_000, "output_tokens": 100} | usage
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(content),
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": counts,
    }


class ScriptedClient:
    """Returns (or raises) the scripted items in order and records every request."""

    def __init__(self, *items: object) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.items:
            raise AssertionError("the loop called the model more often than scripted")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return copy.deepcopy(item)  # type: ignore[arg-type]


# --- Test-local handlers -------------------------------------------------------------


class Handlers:
    """search_client_docs and get_portfolio, recording what they were called with."""

    def __init__(self, hit_text: str = "Total assets EUR 1,250,000.00.") -> None:
        self.hit_text = hit_text
        self.calls: list[tuple[str, str, BaseModel]] = []

    def search(self, advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        self.calls.append(("search_client_docs", advisor.advisor_id, arguments))
        hit = DocumentHit(
            document_id="doc-1",
            mandate_id=MANDATE,
            title="Report",
            lang="en",
            excerpt=self.hit_text,
        )
        return Untrusted([hit], Provenance(tool="search_client_docs", record_ids=("doc-1",)))

    def portfolio(self, advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        self.calls.append(("get_portfolio", advisor.advisor_id, arguments))
        value = Portfolio(
            mandate_id=MANDATE,
            document_id="doc-2",
            as_of=datetime.date(2026, 3, 5),
            report="Equity share 42.5%",
        )
        return Untrusted(value, Provenance(tool="get_portfolio", record_ids=("doc-2",)))

    def mapping(self) -> dict[str, loop.Handler]:
        return {"search_client_docs": self.search, "get_portfolio": self.portfolio}


def run(client: ScriptedClient, handlers: Handlers | None = None, **kwargs: Any) -> loop.LoopResult:
    handlers = handlers or Handlers()
    arguments = {"question": "What do I need to know before the call?"} | kwargs
    return loop.run(
        issue(ADVISOR),
        arguments.pop("question"),
        arguments.pop("mandate_id", None),
        client=client,
        handlers=handlers.mapping(),
        model=MODEL,
    )


def tool_results(call: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks of the last message of a recorded request."""
    last = call["messages"][-1]
    assert last["role"] == "user"
    return [block for block in last["content"] if block["type"] == "tool_result"]


SEARCH = tool_use("tu_1", "search_client_docs", {"query": "briefing before the call"})
PORTFOLIO = tool_use("tu_2", "get_portfolio", {"mandate_id": MANDATE})


Traced = Callable[[Callable[[], object]], dict[str, Any]]


@pytest.fixture
def traced() -> Iterator[Traced]:
    """Runs a callable inside a recording span and returns the span's attributes. The
    span is exported unfiltered: the loop itself must put no text in any attribute,
    before the allowlist ever sees it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    def call(body: Callable[[], object]) -> dict[str, Any]:
        with tracer.start_as_current_span("POST /api/ask"):
            body()
        [span] = exporter.get_finished_spans()
        exporter.clear()
        assert CANARY not in span.to_json(), "a canary in the span"
        return dict(span.attributes or {})

    yield call
    provider.shutdown()


# --- Stop reasons --------------------------------------------------------------------


def test_an_answer_ends_the_turn() -> None:
    client = ScriptedClient(response(text("All set.")))
    result = run(client)
    assert (result.answer, result.stop, result.turns) == ("All set.", "end_turn", 1)
    assert len(client.calls) == 1


def test_a_stop_sequence_ends_the_turn_too() -> None:
    result = run(ScriptedClient(response(text("Done."), stop="stop_sequence")))
    assert (result.answer, result.stop) == ("Done.", "end_turn")


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded"])
def test_a_cut_answer_says_so(reason: str) -> None:
    # Never a silent truncation: the stop names the budget, the partial text stays. The
    # tool call in a cut reply may itself be cut: it does not run (review finding test-2).
    handlers = Handlers()
    result = run(ScriptedClient(response(text("Partial"), SEARCH, stop=reason)), handlers)
    assert (result.answer, result.stop) == ("Partial", "budget")
    assert handlers.calls == []


def test_a_refusal_ends_the_turn_without_an_answer() -> None:
    handlers = Handlers()
    result = run(ScriptedClient(response(text("I will not"), SEARCH, stop="refusal")), handlers)
    assert (result.answer, result.stop) == ("", "refusal")
    assert handlers.calls == []


@pytest.mark.parametrize("reason", ["pause_turn", None, "something_new"])
def test_an_unexpected_stop_fails_closed(reason: str | None) -> None:
    # pause_turn belongs to server tools, which the loop never offers.
    with pytest.raises(loop.LoopError) as failed:
        run(ScriptedClient(response(text("x"), stop=reason)))
    assert failed.value.kind == "unexpected_stop"


def test_tool_use_without_a_tool_call_fails_closed() -> None:
    with pytest.raises(loop.LoopError) as failed:
        run(ScriptedClient(response(text("x"), stop="tool_use")))
    assert failed.value.kind == "protocol"


# --- Tool calls ----------------------------------------------------------------------


def test_a_tool_call_runs_and_its_result_goes_back() -> None:
    handlers = Handlers()
    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text("Briefing.")))
    result = run(client, handlers)
    assert result.stop == "end_turn" and result.turns == 2
    assert [(name, advisor) for name, advisor, _ in handlers.calls] == [
        ("search_client_docs", ADVISOR)
    ]
    [block] = tool_results(client.calls[1])
    assert block["tool_use_id"] == "tu_1" and block.get("is_error") is not True


def test_parallel_calls_answer_in_one_message_in_order() -> None:
    handlers = Handlers()
    client = ScriptedClient(
        response(text("Looking."), PORTFOLIO, SEARCH, stop="tool_use"), response(text("Done."))
    )
    run(client, handlers)
    last = client.calls[1]["messages"][-1]
    # The ordering rule: only tool_result blocks, one per tool_use, in their order.
    assert [block["type"] for block in last["content"]] == ["tool_result", "tool_result"]
    assert [block["tool_use_id"] for block in last["content"]] == ["tu_2", "tu_1"]
    assert [name for name, _, _ in handlers.calls] == ["get_portfolio", "search_client_docs"]


def test_every_tool_result_is_wrapped() -> None:
    client = ScriptedClient(
        response(PORTFOLIO, SEARCH, tool_use("tu_3", "draft_email", {}), stop="tool_use"),
        response(text("Done.")),
    )
    run(client)
    for block in tool_results(client.calls[1]):
        content = block["content"]
        assert isinstance(content, str) and content.startswith("<untrusted "), block
        assert content.endswith("</untrusted>")


def test_the_assistant_turn_goes_back_unchanged() -> None:
    # Thinking blocks must come back exactly as they were sent (preserved thinking).
    turn = response(thinking("plan", "sig-xyz"), text("Looking."), SEARCH, stop="tool_use")
    client = ScriptedClient(turn, response(text("Done.")))
    run(client)
    assistant = client.calls[1]["messages"][-2]
    assert assistant == {"role": "assistant", "content": turn["content"]}


def test_only_the_two_read_tools_are_offered() -> None:
    client = ScriptedClient(response(text("ok")))
    run(client)
    registry = loop.read_registry()
    assert registry.names() == ("search_client_docs", "get_portfolio")
    assert client.calls[0]["tools"] == [
        {
            "name": name,
            "description": registry.spec(name).description,
            "input_schema": registry.schema(name),
        }
        for name in registry.names()
    ]


def test_a_request_carries_exactly_the_contract_fields() -> None:
    client = ScriptedClient(response(text("ok")))
    run(client)
    [call] = client.calls
    assert set(call) == {"system", "messages", "tools", "max_tokens"}  # no tool_choice
    assert call["system"] == prompt.SYSTEM_PROMPT
    assert call["max_tokens"] == loop.MAX_TOKENS


@pytest.mark.parametrize("name", ["draft_email", "create_followup", "web_fetch", CANARY])
def test_a_tool_outside_the_registry_is_refused(name: str) -> None:
    handlers = Handlers()
    client = ScriptedClient(
        response(tool_use("tu_9", name, {"to": ["x"]}), stop="tool_use"), response(text("ok"))
    )
    run(client, handlers)
    assert handlers.calls == []
    [block] = tool_results(client.calls[1])
    assert block["is_error"] is True and "unknown_tool" in block["content"]


def test_a_rejected_call_names_the_error_types_and_locations() -> None:
    handlers = Handlers()
    arguments = {"query": "x", "berater_id": "someone", "limit": 99}
    client = ScriptedClient(
        response(tool_use("tu_1", "search_client_docs", arguments), stop="tool_use"),
        response(text("ok")),
    )
    run(client, handlers)
    assert handlers.calls == []
    [block] = tool_results(client.calls[1])
    assert block["is_error"] is True
    for needle in ("identity_argument", "berater_id", "less_than_equal", "limit"):
        assert needle in block["content"], needle


def test_a_failing_handler_gives_an_error_result_and_the_turn_goes_on() -> None:
    def broken(advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        raise RuntimeError(CANARY)

    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text("Sorry.")))
    result = loop.run(
        issue(ADVISOR),
        "q",
        client=client,
        handlers={"search_client_docs": broken, "get_portfolio": Handlers().portfolio},
        model=MODEL,
    )
    [block] = tool_results(client.calls[1])
    assert block["is_error"] is True and "tool_failed" in block["content"]
    # The class name goes to the span only (review finding sec-8).
    assert "RuntimeError" not in block["content"] and CANARY not in block["content"]
    assert result.stop == "end_turn"


def test_a_handler_must_return_untrusted() -> None:
    def plain(advisor: VerifiedAdvisor, arguments: BaseModel) -> Any:
        return "EUR 5"

    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text("x")))
    with pytest.raises(loop.LoopError) as failed:
        loop.run(
            issue(ADVISOR),
            "q",
            client=client,
            handlers={"search_client_docs": plain, "get_portfolio": Handlers().portfolio},
            model=MODEL,
        )
    assert failed.value.kind == "handler"


def test_the_handlers_must_be_the_registrys() -> None:
    with pytest.raises(ValueError, match="handlers"):
        loop.run(
            issue(ADVISOR),
            "q",
            client=ScriptedClient(),
            handlers={"search_client_docs": Handlers().search},
            model=MODEL,
        )


# --- The message-block check (PR 6's merge gate) -------------------------------------

REFUSED_BLOCKS = {
    "a server tool call": {"type": "server_tool_use", "id": "s", "name": "web_fetch", "input": {}},
    "a fetch result": {"type": "web_fetch_tool_result", "tool_use_id": "s", "content": {}},
    "a document": {"type": "document", "source": {"type": "url", "url": "https://x.example"}},
    "an image": {"type": "image", "source": {"type": "url", "url": "https://x.example/i.png"}},
    "a text with citations": {"type": "text", "text": "x", "citations": [{"type": "x"}]},
    "a text with a source": {"type": "text", "text": "x", "source": {"type": "url"}},
    "a tool call from a toolset": tool_use("t", "search_client_docs", {"query": "x"})
    | {"toolset_name": "mcp"},
    "a programmatic caller": tool_use("t", "search_client_docs", {"query": "x"})
    | {"caller": {"type": "code_execution_20260120"}},
    "thinking with an extra field": thinking() | {"source": "x"},
    "an unknown block": {"type": "container_upload", "file_id": "f"},
}


@pytest.mark.parametrize("block", list(REFUSED_BLOCKS.values()), ids=list(REFUSED_BLOCKS))
def test_a_refused_block_ends_the_request_before_the_next_call(block: dict[str, Any]) -> None:
    handlers = Handlers()
    client = ScriptedClient(response(block, SEARCH, stop="tool_use"), response(text("x")))
    with pytest.raises(loop.LoopError) as failed:
        run(client, handlers)
    assert failed.value.kind == "message_refused"
    assert len(client.calls) == 1
    assert handlers.calls == [], "a refused reply ran a tool (review finding test-2)"


ALLOWED_BLOCKS = {
    "text": text("x"),
    "thinking": thinking("x"),
    "redacted thinking": {"type": "redacted_thinking", "data": "opaque"},
    "a direct caller": tool_use("tu_8", "get_portfolio", {"mandate_id": MANDATE})
    | {"caller": {"type": "direct"}, "toolset_name": None},
}


@pytest.mark.parametrize("block", list(ALLOWED_BLOCKS.values()), ids=list(ALLOWED_BLOCKS))
def test_the_allowed_blocks_pass(block: dict[str, Any]) -> None:
    stop = "tool_use" if block["type"] == "tool_use" else "end_turn"
    items = [response(block, stop=stop)] + ([response(text("ok"))] if stop == "tool_use" else [])
    assert run(ScriptedClient(*items)).stop == "end_turn"


def test_a_source_key_inside_tool_input_is_data() -> None:
    # The model's arguments are data, not a block: a "source" key there fetches nothing,
    # and the registry refuses it as an extra key.
    odd = tool_use("tu_1", "search_client_docs", {"query": "x", "source": {"type": "url"}})
    client = ScriptedClient(response(odd, stop="tool_use"), response(text("ok")))
    assert run(client).stop == "end_turn"
    [block] = tool_results(client.calls[1])
    assert block["is_error"] is True and "extra_forbidden" in block["content"]


# --- Limits --------------------------------------------------------------------------


def test_the_turn_limit_ends_with_a_stated_stop() -> None:
    items = [response(SEARCH, stop="tool_use") for _ in range(loop.MAX_TURNS)]
    client = ScriptedClient(*items)
    result = run(client)
    assert (result.stop, result.turns) == ("max_turns", loop.MAX_TURNS)
    assert len(client.calls) == loop.MAX_TURNS
    # The last turn's calls do not run, and the text before a tool call is no answer.
    assert result.answer == ""


def test_the_budget_ends_the_turn_after_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 30_000)
    client = ScriptedClient(
        response(SEARCH, stop="tool_use", input_tokens=22_000, output_tokens=200),
        response(text("never")),
    )
    result = run(client)
    # 22,200 spent; the next call's worst case (its input plus MAX_TOKENS) does not fit.
    assert (result.stop, len(client.calls)) == ("budget", 1)


def test_the_budget_counts_what_wrap_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 12_000)
    handlers = Handlers(hit_text="x" * 40_000)  # some 13,000 tokens once wrapped
    client = ScriptedClient(
        response(SEARCH, stop="tool_use", input_tokens=100), response(text("x"))
    )
    result = run(client, handlers)
    assert (result.stop, len(client.calls)) == ("budget", 1)


def test_a_failed_call_is_charged_its_worst_case(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR 6 handover (sec-6): a timeout may still bill input plus max_tokens.
    charged: list[int] = []
    original = loop.RequestBudget.charge

    def spy(self: loop.RequestBudget, tokens: int) -> None:
        charged.append(tokens)
        original(self, tokens)

    monkeypatch.setattr(loop.RequestBudget, "charge", spy)
    with pytest.raises(loop.LoopError):
        run(ScriptedClient(ModelCallError("timeout")))
    assert charged and charged[-1] >= loop.MAX_TOKENS


def test_the_budget_never_goes_up() -> None:
    budget = loop.RequestBudget(100)
    budget.charge(40)
    assert budget.remaining() == 60
    with pytest.raises(ValueError):
        budget.charge(-1)
    with pytest.raises(loop.BudgetExceeded):
        budget.charge(61)
    assert budget.remaining() == 60


@pytest.mark.parametrize(
    ("question", "mandate_id"),
    [("", None), ("x" * 2_001, None), (5, None), ("q", ""), ("q", "m" * 129), ("q", 5)],
)
def test_the_inputs_are_bounded(question: Any, mandate_id: Any) -> None:
    with pytest.raises(ValueError):
        run(ScriptedClient(), question=question, mandate_id=mandate_id)


def test_the_mandate_goes_to_the_model_as_context() -> None:
    client = ScriptedClient(response(text("ok")))
    run(client, mandate_id=MANDATE)
    first = client.calls[0]["messages"][0]
    assert first["role"] == "user" and MANDATE in json.dumps(first["content"])


# --- Model failures ------------------------------------------------------------------


@pytest.mark.parametrize("failure", [ModelCallError("connection"), ModelCallError("status", 529)])
def test_a_transient_failure_is_retried_once(failure: ModelCallError) -> None:
    client = ScriptedClient(failure, response(text("ok")))
    assert run(client).stop == "end_turn"
    assert len(client.calls) == 2


def test_a_second_transient_failure_ends_the_request() -> None:
    client = ScriptedClient(ModelCallError("status", 503), ModelCallError("status", 503))
    with pytest.raises(loop.LoopError) as failed:
        run(client)
    assert failed.value.kind == "model_call"


@pytest.mark.parametrize(
    "failure",
    [ModelCallError("response"), ModelCallError("timeout"), ModelCallError("status", 400)],
)
def test_other_failures_are_not_retried(failure: ModelCallError) -> None:
    # PR 6 handover (sec-r7): "response" is not retried; nor is a timeout, which may bill.
    client = ScriptedClient(failure, response(text("ok")))
    with pytest.raises(loop.LoopError) as failed:
        run(client)
    assert failed.value.kind == "model_call" and len(client.calls) == 1


def test_an_unpriced_answer_ends_the_request() -> None:
    # PR 6 handover: a usage the table cannot price fails closed.
    unpriced = response(text("x"))
    unpriced["usage"]["service_tier"] = "priority"
    with pytest.raises(loop.LoopError) as failed:
        run(ScriptedClient(unpriced))
    assert failed.value.kind == "price_unknown"


def test_a_loop_error_carries_no_text() -> None:
    error = loop.LoopError("model_call")
    assert str(error) == "model_call" and error.kind == "model_call"


# --- The result ----------------------------------------------------------------------


def test_grounding_and_citations_come_from_this_requests_results() -> None:
    answer = "Assets of EUR 1,250,000.00 [source: doc-1], equity 42.5% [source: doc-2, doc-9]."
    client = ScriptedClient(response(SEARCH, PORTFOLIO, stop="tool_use"), response(text(answer)))
    result = run(client)
    assert result.citations == ("doc-1", "doc-2")
    grounding = result.grounding
    assert (grounding.figures, grounding.unmatched, grounding.unknown_citations) == (2, 0, 1)


def test_usage_and_cost_add_up_over_the_calls() -> None:
    first = response(SEARCH, stop="tool_use", input_tokens=1_200, output_tokens=80)
    second = response(text("ok"), input_tokens=2_000, output_tokens=300)
    result = run(ScriptedClient(first, second))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3_200, 380)
    expected = cost_usd(MODEL, first["usage"]) + cost_usd(MODEL, second["usage"])
    assert result.cost_usd == expected and isinstance(result.cost_usd, Decimal)


def test_the_answer_joins_the_text_blocks() -> None:
    result = run(ScriptedClient(response(thinking("x"), text("One. "), text("Two."))))
    assert result.answer == "One. Two."


# --- The system prompt ---------------------------------------------------------------


def test_the_prompt_is_versioned_and_built_from_branding() -> None:
    assert prompt.PROMPT_VERSION == "v1"
    for needle in (branding.PRODUCT.name, branding.BANK.display_name, CITATION_FORM):
        assert needle in prompt.SYSTEM_PROMPT, needle
    assert "untrusted" in prompt.SYSTEM_PROMPT
    assert "investment advice" in prompt.SYSTEM_PROMPT
    assert prompt.PROMPT_SHA256 == hashlib.sha256(prompt.SYSTEM_PROMPT.encode()).hexdigest()


def test_the_prompt_names_no_cast_member_or_literal_domain() -> None:
    # Nothing depends on it staying secret, and it carries no client data.
    for person in branding.CAST:
        assert person.name not in prompt.SYSTEM_PROMPT
    for domain in (branding.DOMAINS.staff, branding.DOMAINS.attacker, branding.DOMAINS.canary):
        assert domain not in prompt.SYSTEM_PROMPT


# --- Traces --------------------------------------------------------------------------


def test_the_span_carries_the_approved_attributes(traced: Traced) -> None:
    answer = "EUR 1,250,000.00 and EUR 7.00 [source: doc-1] [source: nope]"
    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text(answer)))
    attributes = traced(lambda: run(client))
    assert set(attributes) <= tracing.LOOP_KEYS
    assert attributes[key("prompt.version")] == "v1"
    assert attributes[key("prompt.sha256")] == prompt.PROMPT_SHA256
    assert attributes["gen_ai.request.model"] == MODEL
    assert attributes["gen_ai.usage.input_tokens"] == 2_000
    assert attributes["gen_ai.usage.output_tokens"] == 200
    assert (attributes[key("loop.stop")], attributes[key("loop.turns")]) == ("end_turn", 2)
    assert tuple(attributes[key("loop.tools")]) == ("search_client_docs",)
    assert attributes[key("grounding.figures")] == 2
    assert attributes[key("grounding.unmatched")] == 1
    assert attributes[key("grounding.unknown_citations")] == 1


def test_counts_on_the_span_are_capped(traced: Traced) -> None:
    # Review finding sec-4 of PR 9: the model steers these counts; capped, they carry
    # little.
    answer = " ".join(f"[source: x-{n}]" for n in range(500))
    attributes = traced(lambda: run(ScriptedClient(response(text(answer)))))
    assert attributes[key("grounding.unknown_citations")] == loop.SPAN_COUNT_CAP


def test_a_canary_reaches_no_span_and_no_log(
    traced: Traced, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    refused = tool_use("tu_7", "search_client_docs", {"query": "x", f"{CANARY}_key": 1})
    unknown = tool_use("tu_8", f"{CANARY}_tool", {})
    client = ScriptedClient(
        response(SEARCH, refused, unknown, stop="tool_use"),
        response(text(f"The answer is {CANARY}.")),
    )
    handlers = Handlers(hit_text=f"A mail says {CANARY}.")
    results: list[loop.LoopResult] = []
    attributes = traced(lambda: results.append(run(client, handlers, question=f"Ask {CANARY}")))
    # The key name goes back to the model, in its tool_result ...
    assert any(f"{CANARY}_key" in block["content"] for block in tool_results(client.calls[1]))
    assert CANARY in results[0].answer
    # ... and nowhere else (the fixture checks the span).
    assert CANARY not in caplog.text
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    assert attributes[key("loop.tool_errors")], "non-vacuity: the refusals are traced"
    assert "unknown_tool" in attributes[key("loop.tool_errors")]


def test_a_failure_is_traced_by_type(traced: Traced) -> None:
    def fail() -> None:
        with pytest.raises(loop.LoopError):
            run(ScriptedClient(ModelCallError("response", cause="JSONDecodeError")))

    attributes = traced(fail)
    assert attributes[key("loop.error")] == "model_call"
    assert attributes[key("loop.error_cause")] == "response.JSONDecodeError"


def test_the_trace_allowlist_admits_the_loop_keys() -> None:
    assert tracing.LOOP_KEYS <= tracing.ALLOWED_KEYS
    assert tracing.LOOP_KEYS == {
        key("prompt.version"),
        key("prompt.sha256"),
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        key("loop.stop"),
        key("loop.turns"),
        key("loop.tools"),
        key("loop.tool_errors"),
        key("loop.error"),
        key("loop.error_cause"),
        key("grounding.figures"),
        key("grounding.unmatched"),
        key("grounding.unknown_citations"),
    }


# --- Review round 1 (PR 10) ----------------------------------------------------------


def test_a_refusal_over_the_budget_is_still_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-3: the stop names why the turn ended; a refusal keeps no text.
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 20_000)
    result = run(ScriptedClient(response(text("No."), stop="refusal", input_tokens=30_000)))
    assert (result.answer, result.stop) == ("", "refusal")


def test_a_final_answer_over_the_budget_ends_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-3: a finished answer is not relabelled; the budget only stops further calls.
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 20_000)
    result = run(ScriptedClient(response(text("Done."), input_tokens=30_000)))
    assert (result.answer, result.stop) == ("Done.", "end_turn")


def test_a_charge_of_exactly_the_rest_fits() -> None:
    budget = loop.RequestBudget(100)
    budget.charge(100)
    assert budget.remaining() == 0


def test_the_wrapped_result_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-1: the charge itself, not the next call's estimate.
    charged: list[int] = []
    original = loop.RequestBudget.charge

    def spy(self: loop.RequestBudget, tokens: int) -> None:
        charged.append(tokens)
        original(self, tokens)

    monkeypatch.setattr(loop.RequestBudget, "charge", spy)
    handlers = Handlers(hit_text="y" * 9_000)
    run(ScriptedClient(response(SEARCH, stop="tool_use"), response(text("ok"))), handlers)
    # 9,000 characters are 3,000 tokens raw; the wrapped result, with its delimiter and
    # the hit's other fields, is more (round 2: a charge of the raw text would not do).
    raw = loop._tokens(handlers.hit_text)
    assert any(tokens > raw + 20 and tokens != 1_100 for tokens in charged), (raw, charged)


def test_a_budget_stop_between_calls_has_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # spec-8: the text before a tool call is no answer.
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 30_000)
    client = ScriptedClient(
        response(text("Looking."), SEARCH, stop="tool_use", input_tokens=22_000),
        response(text("x")),
    )
    result = run(client)
    assert (result.answer, result.stop) == ("", "budget")


def test_non_ascii_text_is_not_undercounted() -> None:
    # sec-3: a character outside ASCII may be several tokens; each UTF-8 byte counts.
    assert loop._tokens("a" * 30) == 10
    assert loop._tokens("\U0001d400" * 30) >= 120
    assert loop._tokens("ä" * 30) >= 60


def test_a_non_ascii_result_meets_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "REQUEST_BUDGET", 40_000)
    handlers = Handlers(hit_text="\U0001d400" * 9_000)  # 36,000 bytes, 3,000 by characters
    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text("x")))
    result = run(client, handlers)
    assert (result.stop, len(client.calls)) == ("budget", 1)


def test_calls_per_turn_are_capped() -> None:
    # sec-2: one injected mail must not fan out into hundreds of searches.
    handlers = Handlers()
    many = [tool_use(f"tu_{n}", "search_client_docs", {"query": f"q{n}"}) for n in range(12)]
    client = ScriptedClient(response(*many, stop="tool_use"), response(text("ok")))
    run(client, handlers)
    assert len(handlers.calls) == loop.MAX_CALLS_PER_TURN
    blocks = tool_results(client.calls[1])
    assert [block["tool_use_id"] for block in blocks] == [f"tu_{n}" for n in range(12)]
    limited = blocks[loop.MAX_CALLS_PER_TURN :]
    assert all(block["is_error"] and "call_limit" in block["content"] for block in limited)


def test_calls_per_request_are_capped() -> None:
    # Round 2: five calls a turn, so the cap of 24 falls inside the fifth turn.
    handlers = Handlers()
    turn = [tool_use(f"tu_{n}", "search_client_docs", {"query": "q"}) for n in range(5)]
    client = ScriptedClient(
        *[response(*turn, stop="tool_use") for _ in range(5)], response(text("ok"))
    )
    run(client, handlers)
    assert len(handlers.calls) == loop.MAX_CALLS_PER_REQUEST
    fifth = tool_results(client.calls[5])
    assert [block.get("is_error", False) for block in fifth] == [False] * 4 + [True]
    assert "call_limit" in fifth[-1]["content"]


def test_the_span_lists_carry_no_order(traced: Traced) -> None:
    # sec-1: the model chooses which calls fail and in which order; the span keeps a
    # sorted set, so neither order nor repetition carries anything.
    def turn(order: list[dict[str, Any]]) -> Callable[[], object]:
        return lambda: run(ScriptedClient(response(*order, stop="tool_use"), response(text("ok"))))

    bad_limit = tool_use("tu_1", "search_client_docs", {"query": "x", "limit": 99})
    bad_extra = tool_use("tu_2", "search_client_docs", {"query": "x", "other": 1})
    first = traced(turn([bad_limit, bad_extra, PORTFOLIO, SEARCH]))
    second = traced(turn([SEARCH, bad_extra, PORTFOLIO, bad_limit, bad_limit]))
    for name in ("tools", "tool_errors"):
        assert tuple(first[key(f"loop.{name}")]) == tuple(second[key(f"loop.{name}")]), name
    assert tuple(first[key("loop.tool_errors")]) == ("extra_forbidden", "less_than_equal")


def test_usage_is_counted_when_a_reply_is_refused(traced: Traced) -> None:
    # sec-4: the refused reply was paid for.
    block = {"type": "server_tool_use", "id": "s", "name": "web_fetch", "input": {}}

    def body() -> None:
        with pytest.raises(loop.LoopError):
            run(ScriptedClient(response(block, input_tokens=5_000, output_tokens=7)))

    attributes = traced(body)
    assert (attributes[key("loop.error")], attributes["gen_ai.usage.input_tokens"]) == (
        "message_refused",
        5_000,
    )


def test_an_unexpected_exception_becomes_a_traced_loop_error(traced: Traced) -> None:
    class Odd(Exception):
        pass

    def body() -> None:
        with pytest.raises(loop.LoopError) as failed:
            run(ScriptedClient(Odd(CANARY)))
        error = failed.value
        assert error.kind == "internal" and error.__cause__ is None
        # Round 2, N1: not even the implicit context keeps the original's message.
        assert error.__context__ is None
        assert CANARY not in "".join(traceback.format_exception(error))

    attributes = traced(body)
    assert (attributes[key("loop.error")], attributes[key("loop.error_cause")]) == (
        "internal",
        "Odd",
    )


def test_a_successful_retry_leaves_no_error_on_the_span(traced: Traced) -> None:
    # sec-5.
    client = ScriptedClient(ModelCallError("status", 529), response(text("ok")))
    attributes = traced(lambda: run(client))
    assert key("loop.error_cause") not in attributes and key("loop.error") not in attributes


def test_a_status_failure_names_its_code(traced: Traced) -> None:
    def body() -> None:
        with pytest.raises(loop.LoopError):
            run(ScriptedClient(ModelCallError("status", 401)))

    assert traced(body)[key("loop.error_cause")] == "status.401"


@pytest.mark.parametrize("status", sorted(loop.TRANSIENT_STATUS))
def test_every_transient_status_is_retried_with_the_same_request(status: int) -> None:
    client = ScriptedClient(ModelCallError("status", status), response(text("ok")))
    assert run(client).stop == "end_turn"
    assert client.calls[0] == client.calls[1]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, None])
def test_other_statuses_are_not_retried(status: int | None) -> None:
    client = ScriptedClient(ModelCallError("status", status), response(text("ok")))
    with pytest.raises(loop.LoopError):
        run(client)
    assert len(client.calls) == 1


def test_a_handler_result_must_name_its_tool() -> None:
    # sec-9: a handler that returns another tool's provenance is a programming error.
    def wrong(advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        return Untrusted("x", Provenance(tool="get_portfolio", record_ids=("doc-1",)))

    client = ScriptedClient(response(SEARCH, stop="tool_use"), response(text("x")))
    with pytest.raises(loop.LoopError) as failed:
        loop.run(
            issue(ADVISOR),
            "q",
            client=client,
            handlers={"search_client_docs": wrong, "get_portfolio": Handlers().portfolio},
            model=MODEL,
        )
    assert failed.value.kind == "handler"


def test_a_failing_handler_is_traced_by_class(traced: Traced) -> None:
    def broken(advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        raise LookupError(CANARY)

    def body() -> None:
        loop.run(
            issue(ADVISOR),
            "q",
            client=ScriptedClient(response(SEARCH, stop="tool_use"), response(text("ok"))),
            handlers={"search_client_docs": broken, "get_portfolio": Handlers().portfolio},
            model=MODEL,
        )

    assert "LookupError" in traced(body)[key("loop.tool_errors")]


def test_the_result_repr_hides_the_answer() -> None:
    # sec-10.
    result = run(ScriptedClient(response(text(f"Secret {CANARY}"))))
    assert CANARY in result.answer and CANARY not in repr(result)


def test_a_reply_from_another_model_ends_the_request() -> None:
    # spec-5: the cost and the trace name the configured model; a reply from another
    # one would make both wrong.
    other = response(text("x"))
    other["model"] = "claude-opus-5-5"
    with pytest.raises(loop.LoopError) as failed:
        run(ScriptedClient(other))
    assert failed.value.kind == "protocol"


@pytest.mark.parametrize("mandate_id", ["a b", "a\nb", "a\u2028b", "a\u00a0b", "../x", "x]"])
def test_a_mandate_id_has_the_id_form(mandate_id: str) -> None:
    # spec-10.
    with pytest.raises(ValueError):
        run(ScriptedClient(), mandate_id=mandate_id)


def test_the_first_message_has_an_exact_form() -> None:
    client = ScriptedClient(response(text("ok")), response(text("ok")))
    run(client, question="Q?")
    run(client, question="Q?", mandate_id=MANDATE)
    assert client.calls[0]["messages"][0]["content"] == "Q?"
    assert client.calls[1]["messages"][0]["content"] == f"Q?\n\nMandate in focus: {MANDATE}"


def test_cached_tokens_count_as_input() -> None:
    usage = {"cache_read_input_tokens": 5_000, "cache_creation_input_tokens": 200}
    reply = response(text("ok"), input_tokens=100, **usage)
    result = run(ScriptedClient(reply))
    assert result.usage.input_tokens == 5_300
    assert result.cost_usd == cost_usd(MODEL, reply["usage"])


@pytest.mark.parametrize(
    "usage", [None, {"input_tokens": -1}, {"input_tokens": "5"}, {"output_tokens": 1.5}]
)
def test_an_unreadable_usage_is_a_protocol_error(usage: Any) -> None:
    reply = response(text("ok"))
    reply["usage"] = usage
    with pytest.raises(loop.LoopError) as failed:
        run(ScriptedClient(reply))
    assert failed.value.kind in ("protocol", "price_unknown")


def test_the_trace_id_is_the_spans(traced: Traced) -> None:
    results: list[loop.LoopResult] = []
    traced(lambda: results.append(run(ScriptedClient(response(text("ok"))))))
    assert results[0].trace_id is not None and len(results[0].trace_id) == 32
    assert run(ScriptedClient(response(text("ok")))).trace_id is None


USER_BLOCKS = {
    "a list in a tool result": {
        "type": "tool_result",
        "tool_use_id": "t",
        "content": [{"type": "document", "source": {"type": "url", "url": "https://x.example"}}],
    },
    "is_error as a number": {
        "type": "tool_result",
        "tool_use_id": "t",
        "content": "x",
        "is_error": 1,
    },
    "a cache marker": {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}},
    "an image": {"type": "image", "source": {"type": "url", "url": "https://x.example/i.png"}},
}


@pytest.mark.parametrize("block", list(USER_BLOCKS.values()), ids=list(USER_BLOCKS))
def test_the_user_side_blocks_are_checked(block: dict[str, Any]) -> None:
    with pytest.raises(loop.LoopError):
        loop.check_messages([{"role": "user", "content": [block]}])


def test_a_message_has_role_and_content_only() -> None:
    with pytest.raises(loop.LoopError):
        loop.check_messages([{"role": "user", "content": "q", "name": "x"}])
    loop.check_messages([{"role": "user", "content": "q"}])


def test_the_prompt_names_no_cast_member_as_a_word() -> None:
    # test-14: word boundaries, so a longer word that starts with a name does not count.
    import re

    for person in branding.CAST:
        assert not re.search(rf"\b{re.escape(person.name)}\b", prompt.SYSTEM_PROMPT), person.name


def test_the_prompt_names_the_read_tools_from_the_registry() -> None:
    # spec-12.
    for name in loop.read_registry().names():
        assert name in prompt.SYSTEM_PROMPT


def test_the_real_client_s_reply_passes_through_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-8: what AnthropicModelClient returns (to_dict, strict validation) is what the
    # block allowlist sees, and the assistant turn goes back as the API sent it.
    import httpx2

    from app.config.settings import KEY_VARIABLE, Settings
    from app.harness.model_client import build_model_client

    monkeypatch.setenv(KEY_VARIABLE, "cnry-key-not-real")
    first = response(thinking("plan", "sig-a"), text("Looking."), SEARCH, stop="tool_use")
    first["usage"] |= {"cache_read_input_tokens": 0, "service_tier": "standard"}
    second = response(text("Briefing [source: doc-1]."))
    bodies = [first, second]
    sent: list[dict[str, Any]] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=bodies[len(sent) - 1])

    client = build_model_client(Settings(_env_file=None), transport=httpx2.MockTransport(answer))
    result = loop.run(
        issue(ADVISOR), "q", client=client, handlers=Handlers().mapping(), model=client.model
    )
    assert (result.stop, result.citations) == ("end_turn", ("doc-1",))
    assert "tool_choice" not in sent[0]
    assistant = sent[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert [block["type"] for block in assistant["content"]] == ["thinking", "text", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig-a"


# Review finding test-14: any edit to the prompt's text changes this hash and fails here,
# so it comes with a new PROMPT_VERSION (and new recordings, M1 PR 16), never silently.
PROMPT_V1_SHA256 = "6aef4295bb959585a20e556962e477798ce8987d0be2e0b702cd47e9291faf9a"


def test_the_v1_prompt_is_pinned() -> None:
    assert prompt.PROMPT_VERSION == "v1"
    assert prompt.PROMPT_SHA256 == PROMPT_V1_SHA256
