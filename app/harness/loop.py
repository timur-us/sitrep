"""The agent loop (ADR 0001; M1 plan, PR 10): one advisor question, one answer.

run(advisor, question, mandate_id) calls the model, runs the READ tools it asks for, and
returns the answer with its citations, grounding, usage, cost, stop and trace id. Every
control sits here, between the model and the tools, where tests reach it:

- **Tools.** The registry holds search_client_docs and get_portfolio only (read_registry),
  until M1 PR 14 adds the proposal path. The model sees exactly the registry's schemas,
  and no tool_choice (Sonnet 5.5 refuses a forced one). A call is validated by the
  registry; a RejectedCall goes back to the model as error types, locations and the
  registry's fixed messages, and its handler never runs. The advisor comes from the
  login (CLAUDE.md rule 8), never from an argument.
- **Results.** Every tool result enters the context through wrap(), errors included, so
  each tool_result is inside `<untrusted …>` (CLAUDE.md rule 9). The tool_result blocks
  of one turn go back in one user message, alone and in the order of the tool_use blocks.
- **Blocks.** Before every call, every block of every message is checked against an
  allowlist: text without citations, tool_use called directly, thinking and
  redacted_thinking, tool_result with text content. Anything else (a server tool, a
  document or image with a source, a citation, a toolset) ends the request: such a block
  would make the API fetch or run something (PR 6 handover sec-r3, a merge gate). The
  assistant's turn goes back exactly as it came, so thinking blocks stay valid.
- **Stops.** end_turn and stop_sequence end the turn; max_tokens and
  model_context_window_exceeded end it with stop "budget" and the partial text, never a
  silent truncation; refusal ends it with no answer. pause_turn (a server tool's) and any
  other reason raise LoopError: the loop offers no server tool, so the request is
  outside its contract.
- **Limits.** MAX_TURNS model turns per request, MAX_CALLS_PER_TURN and
  MAX_CALLS_PER_REQUEST tool calls (a call past them gets a call_limit error and its
  handler does not run). A RequestBudget of REQUEST_BUDGET tokens is charged with the
  usage after every call and with the size of every wrap() output; before every call
  the worst case (the estimated input plus MAX_TOKENS) must fit, and a failed call is
  charged that worst case (PR 6 handover sec-6). The estimate counts ASCII at
  CHARS_PER_TOKEN and every other UTF-8 byte as a token. Running out ends the turn with
  stop "budget" and no answer; a finished reply keeps its own stop. The loop reads no
  clock: the route's deadline (app/api/ask.py, M1 PR 12) wraps the model client and
  raises BudgetExceeded instead of starting a call, which ends the turn the same way.
- **Failures.** One immediate retry for a connection failure or a transient status
  (429, 5xx, 529). A timeout, which may have billed, and a "response" failure are not
  retried (PR 6 handover sec-r7). A usage the price table cannot price ends the request
  (PriceUnknown), and so does a reply from another model than the configured one. A
  handler that refuses a call with a reviewed ToolFailed kind (handlers.TOOL_FAILURES)
  gives the model that kind, a fixed word, which goes to the span too (M1 PR 12). Any
  other exception from a handler gives the model a fixed "tool_failed" result, its class
  name going to the span, and the turn goes on; but a GuardError (a row of another
  advisor reached the handler, or the pool refused a connection) ends the request as
  LoopError("guard"), and a database that does not answer (psycopg's OperationalError)
  as LoopError("database"). Any other exception becomes LoopError("internal").
  LoopError carries a kind, no text, and the span is written on every path.
- **Traces.** The request's span gets the attributes of tracing.LOOP and nothing else:
  the prompt's version and hash, the model, token counts, stop, turns, the registered
  tool names called and the error types (sorted sets: order and repetition carry
  nothing), and the grounding counts, capped at SPAN_COUNT_CAP because the model steers
  them (PR 9 review sec-4). Nothing is logged, and LoopResult's repr leaves out the
  answer.

Grounding (app/harness/grounding.py) runs over the answer and the results of this
request; it reports and does not block (plan default 14).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

import psycopg
from opentelemetry import trace
from pydantic import BaseModel

from app.auth.identity import VerifiedAdvisor
from app.db.session import GuardError
from app.harness import grounding
from app.harness.context import BudgetExceeded, Provenance, Untrusted, wrap
from app.harness.grounding import GroundingReport
from app.harness.handlers import TOOL_FAILURES, ToolFailed
from app.harness.model import ModelClient
from app.harness.model_client import ModelCallError, PriceUnknown, cost_usd
from app.harness.mvp_tools import TOOLS
from app.harness.prompt import PROMPT_SHA256, PROMPT_VERSION, SYSTEM_PROMPT
from app.harness.tools import Effect, RejectedCall, ToolRegistry
from app.observability import tracing

__all__ = [
    "BudgetExceeded",
    "Handler",
    "LoopError",
    "LoopResult",
    "RequestBudget",
    "Usage",
    "read_registry",
    "run",
]

MAX_TURNS = 6
# One response's ceiling, thinking included; without streaming, kept well inside the
# client's timeout (default 10 of the M1 plan).
MAX_TOKENS = 8_192
# The tokens one advisor request may spend, all calls and all tool results together.
REQUEST_BUDGET = 150_000
# For estimates only: tool results and the next call's input. Every non-ASCII byte counts
# as a token, an upper bound; ASCII at this ratio is a heuristic (round 2): dense digits
# or base64 may tokenise tighter, and real usage is charged after each call.
CHARS_PER_TOKEN = 3
MAX_QUESTION = 2_000
MAX_MANDATE_ID = 128
# Tool calls the loop runs: per turn and per request (review finding sec-2).
MAX_CALLS_PER_TURN = 8
MAX_CALLS_PER_REQUEST = 24
# Counts on the span are capped, lists shortened: the model steers them.
SPAN_COUNT_CAP = 50
SPAN_LIST_CAP = 20
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 529})
ATTEMPTS = 2
UNKNOWN_TOOL = "unknown_tool"
# An error type or a class name on the span: an identifier, else "other".
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

Stop = Literal["end_turn", "refusal", "budget", "max_turns"]
# Every kind a LoopError may carry (the route maps each to a status, app/api/ask.py).
KINDS = frozenset(
    {
        "model_call",
        "message_refused",
        "unexpected_stop",
        "protocol",
        "handler",
        "price_unknown",
        "guard",
        "database",
        "internal",
    }
)
# What the model is told for a failure it cannot act on (review finding sec-8 of PR 10).
TOOL_FAILED = {"type": "tool_failed", "msg": "the tool failed"}
# The message beside a reviewed ToolFailed kind, which names the reason itself.
TOOL_REFUSED = "the tool refused the call"


class Handler(Protocol):
    """A READ tool's handler: the verified advisor and the validated arguments in, an
    Untrusted result out."""

    def __call__(self, advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]: ...


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts over every call of the request; input includes cached tokens."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LoopResult:
    answer: str = field(repr=False)  # client data: never in a repr (review finding sec-10)
    citations: tuple[str, ...]
    grounding: GroundingReport
    usage: Usage
    cost_usd: Decimal
    stop: Stop
    turns: int
    trace_id: str | None


class LoopError(Exception):
    """The request ended outside its contract. kind is one of KINDS: "model_call",
    "message_refused", "unexpected_stop", "protocol", "handler", "price_unknown",
    "guard" and "database" (a handler's GuardError or OperationalError, M1 PR 12), and
    "internal" (any other exception, raised without it). The error carries nothing else,
    so it is safe in a log line or a span."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class RequestBudget:
    """The TokenBudget of app/harness/context.py: set once, only ever charged."""

    def __init__(self, total: int) -> None:
        self._remaining = total

    def remaining(self) -> int:
        return self._remaining

    def charge(self, tokens: int) -> None:
        if type(tokens) is not int or tokens < 0:
            raise ValueError("a charge is a count of tokens")
        if tokens > self._remaining:
            raise BudgetExceeded("the request's token budget is spent")
        self._remaining -= tokens


def read_registry() -> ToolRegistry:
    """A registry with the READ tools of app/harness/mvp_tools.py, and nothing else."""
    registry = ToolRegistry()
    for spec in TOOLS:
        if spec.effect is Effect.READ:
            registry.register(spec)
    return registry


# --- The block allowlist -------------------------------------------------------------

_ASSISTANT_BLOCKS: Mapping[str, frozenset[str]] = {
    "text": frozenset({"type", "text", "citations"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
    "tool_use": frozenset({"type", "id", "name", "input", "caller", "toolset_name"}),
}
_DIRECT = {"type": "direct"}


def _assistant_block_ok(block: object) -> bool:
    if not isinstance(block, dict) or block.get("type") not in _ASSISTANT_BLOCKS:
        return False
    kind = block["type"]
    if not set(block) <= _ASSISTANT_BLOCKS[kind]:
        return False
    if kind == "text":
        return isinstance(block.get("text"), str) and block.get("citations") is None
    if kind == "thinking":
        return isinstance(block.get("thinking"), str) and isinstance(block.get("signature"), str)
    if kind == "redacted_thinking":
        return isinstance(block.get("data"), str)
    # tool_use, called directly by the model, not from a toolset or a code sandbox.
    return (
        isinstance(block.get("id"), str)
        and isinstance(block.get("name"), str)
        and isinstance(block.get("input"), dict)
        and block.get("caller") in (None, _DIRECT)
        and block.get("toolset_name") is None
    )


def _user_block_ok(block: object) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("type") == "text":
        return set(block) == {"type", "text"} and isinstance(block["text"], str)
    if block.get("type") == "tool_result":
        return (
            set(block) <= {"type", "tool_use_id", "content", "is_error"}
            and isinstance(block.get("tool_use_id"), str)
            and isinstance(block.get("content"), str)
            and type(block.get("is_error", False)) is bool  # 1 == True (review sec-11)
        )
    return False


def check_messages(messages: Sequence[Mapping[str, Any]]) -> None:
    """LoopError("message_refused") unless every block of every message is allowed."""
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if set(message) != {"role", "content"}:
            raise LoopError("message_refused")
        if role == "user" and isinstance(content, str):
            continue
        check = _user_block_ok if role == "user" else _assistant_block_ok
        if role not in ("user", "assistant") or not isinstance(content, list):
            raise LoopError("message_refused")
        if not all(check(block) for block in content):
            raise LoopError("message_refused")


# --- One request ---------------------------------------------------------------------


@dataclass
class _Record:
    """What the span gets: names, types and counts only."""

    tools: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    stop: str | None = None
    error: str | None = None
    cause: str | None = None
    report: GroundingReport | None = None


def _identifier(name: object) -> str:
    return name if isinstance(name, str) and _IDENTIFIER.fullmatch(name) else "other"


_MANDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _check_inputs(question: object, mandate_id: object) -> None:
    if type(question) is not str or not question.strip() or len(question) > MAX_QUESTION:
        raise ValueError(f"the question is a str of 1 to {MAX_QUESTION} characters")
    if mandate_id is not None and (
        type(mandate_id) is not str
        or len(mandate_id) > MAX_MANDATE_ID
        or not _MANDATE_ID.fullmatch(mandate_id)
    ):
        raise ValueError(
            f"a mandate id has up to {MAX_MANDATE_ID} letters, digits, - and _ (review spec-10)"
        )


def _first_message(question: str, mandate_id: str | None) -> str:
    return question if mandate_id is None else f"{question}\n\nMandate in focus: {mandate_id}"


def _tokens(text: str) -> int:
    """An estimate: ASCII at CHARS_PER_TOKEN (a heuristic), and every UTF-8 byte of
    anything else a token of its own, an upper bound (review finding sec-3)."""
    ascii_count = sum(1 for char in text if char < "\x80")
    other = len(text.encode("utf-8")) - ascii_count
    return -(-ascii_count // CHARS_PER_TOKEN) + other  # rounded up


def _estimate(request: Mapping[str, Any]) -> int:
    """A high estimate of the request's input tokens."""
    return _tokens(json.dumps(request, ensure_ascii=False, default=str))


def _text(content: Sequence[Mapping[str, Any]]) -> str:
    return "".join(block["text"] for block in content if block["type"] == "text")


def _usage(usage: object) -> tuple[int, int]:
    if not isinstance(usage, Mapping):
        raise LoopError("protocol")
    counts = []
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key) or 0
        if type(value) is not int or value < 0:
            raise LoopError("protocol")
        counts.append(value)
    output = usage.get("output_tokens") or 0
    if type(output) is not int or output < 0:
        raise LoopError("protocol")
    return sum(counts), output


def _transient(error: ModelCallError) -> bool:
    return error.kind == "connection" or (
        error.kind == "status" and error.status_code in TRANSIENT_STATUS
    )


def _cause(error: ModelCallError) -> str:
    kind = _identifier(error.kind)
    if error.kind == "status" and type(error.status_code) is int:
        return f"{kind}.{error.status_code}"
    return f"{kind}.{_identifier(error.cause)}" if error.cause else kind


def _call(
    client: ModelClient, budget: RequestBudget, request: dict[str, Any], record: _Record
) -> Mapping[str, Any]:
    """One reply, with one immediate retry for a transient failure. A backoff would need
    a clock, which app/ does not read (tests/test_import_allowlist.py)."""
    worst = _estimate(request) + MAX_TOKENS
    for attempt in range(ATTEMPTS):
        try:
            return client.create(**request)
        except ModelCallError as error:
            cause = _cause(error)
            # It may have billed: input plus max_tokens, whatever is left at most.
            budget.charge(min(worst, budget.remaining()))
            retry = _transient(error) and attempt + 1 < ATTEMPTS
            if not retry or budget.remaining() < worst:
                record.cause = cause
                break
    raise LoopError("model_call")


def _refused(name: str, errors: list[dict[str, Any]]) -> Untrusted[Any]:
    return Untrusted({"errors": errors}, Provenance(tool=name, record_ids=()))


def _tool(
    block: Mapping[str, Any],
    advisor: VerifiedAdvisor,
    registry: ToolRegistry,
    handlers: Mapping[str, Handler],
    record: _Record,
) -> tuple[Untrusted[Any], bool]:
    """The tool's result and whether it is an error. Errors carry types, locations and
    the registry's fixed messages: no input value."""
    call = registry.validate_call(block["name"], block["input"])
    if isinstance(call, RejectedCall):
        if call.tool is not None:
            record.tools.add(call.tool)
        record.errors |= {_identifier(error.type) for error in call.errors}
        errors = [
            {"type": error.type, "loc": list(error.loc), "msg": error.msg} for error in call.errors
        ]
        return _refused(call.tool or UNKNOWN_TOOL, errors), True
    name = call.spec.name
    record.tools.add(name)
    record.calls += 1
    ended: str | None = None
    try:
        result = handlers[name](advisor, call.arguments)
    except ToolFailed as error:
        if error.kind in TOOL_FAILURES:
            # A reviewed fixed word, never input: the model may act on it (M1 PR 12).
            record.errors.add(error.kind)
            return _refused(name, [{"type": error.kind, "msg": TOOL_REFUSED}]), True
        record.errors.add(_identifier(type(error).__name__))
        return _refused(name, [TOOL_FAILED]), True
    except GuardError:
        ended = "guard"  # a broken boundary, not a failed call: M2 alarms on it
    except psycopg.OperationalError:
        ended = "database"  # PoolTimeout and QueryCanceled are OperationalErrors too
    except Exception as error:  # noqa: BLE001 - the model learns that it failed, the turn goes on
        # The class name goes to the span only (review finding sec-8).
        record.errors.add(_identifier(type(error).__name__))
        return _refused(name, [TOOL_FAILED]), True
    if ended is not None:
        # Raised outside the except block, so the handler's exception, whose text may
        # name a value, is neither the cause nor the context (as for "internal").
        raise LoopError(ended)
    if type(result) is not Untrusted or result.provenance.tool != name:
        raise LoopError("handler")
    return result, False


def _over_limit(record: _Record, in_turn: int) -> bool:
    """Whether this call is past MAX_CALLS_PER_TURN or MAX_CALLS_PER_REQUEST."""
    return in_turn >= MAX_CALLS_PER_TURN or record.calls >= MAX_CALLS_PER_REQUEST


def run(
    advisor: VerifiedAdvisor,
    question: str,
    mandate_id: str | None = None,
    *,
    client: ModelClient,
    handlers: Mapping[str, Handler],
    model: str,
) -> LoopResult:
    """One advisor question through the loop; see the module docstring."""
    _check_inputs(question, mandate_id)
    registry = read_registry()
    if set(handlers) != set(registry.names()):
        raise ValueError("the handlers must be exactly the registry's tools")
    tools = [
        {
            "name": name,
            "description": registry.spec(name).description,
            "input_schema": registry.schema(name),
        }
        for name in registry.names()
    ]
    budget = RequestBudget(REQUEST_BUDGET)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _first_message(question, mandate_id)}
    ]
    sources: list[Untrusted[Any]] = []
    record = _Record()
    answer = ""
    cost = Decimal(0)
    try:
        while True:
            request = {
                "system": SYSTEM_PROMPT,
                "messages": messages,
                "tools": tools,
                "max_tokens": MAX_TOKENS,
            }
            if budget.remaining() < _estimate(request) + MAX_TOKENS:
                answer, record.stop = "", "budget"
                break
            check_messages(messages)
            reply = _call(client, budget, request, record)
            record.turns += 1
            # The usage first: a refused reply was paid for too (review finding sec-4).
            usage = reply.get("usage")
            spent_in, spent_out = _usage(usage)
            record.input_tokens += spent_in
            record.output_tokens += spent_out
            if reply.get("model") != model:
                raise LoopError("protocol")  # cost and trace would name another model
            try:
                cost += cost_usd(model, usage)  # type: ignore[arg-type]
            except PriceUnknown:
                raise LoopError("price_unknown") from None
            content = reply.get("content")
            if not isinstance(content, list):
                raise LoopError("protocol")
            if not all(_assistant_block_ok(block) for block in content):
                raise LoopError("message_refused")
            try:
                budget.charge(spent_in + spent_out)
                over_budget = False
            except BudgetExceeded:
                over_budget = True
            reason = reply.get("stop_reason")
            # A finished reply keeps its stop; the budget only stops further calls.
            if reason in ("end_turn", "stop_sequence"):
                answer, record.stop = _text(content), "end_turn"
                break
            if reason in ("max_tokens", "model_context_window_exceeded"):
                answer, record.stop = _text(content), "budget"
                break
            if reason == "refusal":
                answer, record.stop = "", "refusal"
                break
            if reason != "tool_use":
                raise LoopError("unexpected_stop")
            calls = [block for block in content if block["type"] == "tool_use"]
            if not calls:
                raise LoopError("protocol")
            # The text before a tool call is no answer (review finding spec-8).
            if over_budget:
                answer, record.stop = "", "budget"
                break
            if record.turns >= MAX_TURNS:
                answer, record.stop = "", "max_turns"
                break
            messages.append({"role": "assistant", "content": content})
            results = []
            for in_turn, block in enumerate(calls):
                if _over_limit(record, in_turn):
                    record.errors.add("call_limit")
                    limit = [{"type": "call_limit", "msg": "too many tool calls"}]
                    named = block["name"] if block["name"] in registry else UNKNOWN_TOOL
                    item, failed = _refused(named, limit), True
                else:
                    item, failed = _tool(block, advisor, registry, handlers, record)
                rendered = wrap(item)
                budget.charge(_tokens(rendered))  # BudgetExceeded ends the turn below
                if not failed:
                    sources.append(item)
                result = {"type": "tool_result", "tool_use_id": block["id"], "content": rendered}
                results.append(result | ({"is_error": True} if failed else {}))
            messages.append({"role": "user", "content": results})
    except BudgetExceeded:
        answer, record.stop = "", "budget"
    except LoopError as error:
        record.error = error.kind
        _trace(record, model)
        raise
    except Exception as error:  # noqa: BLE001 - traced as its class, raised without its text
        record.error, record.cause = "internal", _identifier(type(error).__name__)
    if record.error == "internal":
        # Raised outside the except block, so the original is neither the cause nor the
        # context: its message may carry text (round 2, N1; as model_client.py does).
        _trace(record, model)
        raise LoopError("internal")
    record.report = grounding.check(answer, sources)
    _trace(record, model)
    return LoopResult(
        answer=answer,
        citations=record.report.citations,
        grounding=record.report,
        usage=Usage(record.input_tokens, record.output_tokens),
        cost_usd=cost,
        stop=record.stop,  # type: ignore[arg-type]
        turns=record.turns,
        trace_id=tracing.current_trace_id(),
    )


def _trace(record: _Record, model: str) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    keys = tracing.LOOP
    # Sorted sets: the model chooses which calls fail and in which order, so neither the
    # order nor the repetition goes on the span (review finding sec-1).
    attributes: dict[str, Any] = {
        keys["prompt_version"]: PROMPT_VERSION,
        keys["prompt_sha256"]: PROMPT_SHA256,
        keys["model"]: model,
        keys["input_tokens"]: record.input_tokens,
        keys["output_tokens"]: record.output_tokens,
        keys["turns"]: record.turns,
        keys["tools"]: sorted(record.tools)[:SPAN_LIST_CAP],
        keys["tool_errors"]: sorted(record.errors)[:SPAN_LIST_CAP],
    }
    if record.stop is not None:
        attributes[keys["stop"]] = record.stop
    if record.error is not None:
        attributes[keys["error"]] = record.error
    if record.cause is not None:
        attributes[keys["error_cause"]] = record.cause
    if record.report is not None:
        for name in ("figures", "unmatched", "unknown_citations"):
            attributes[keys[name]] = min(getattr(record.report, name), SPAN_COUNT_CAP)
    span.set_attributes(attributes)
