"""POST /api/ask: one advisor question through the agent loop (M1 plan, PR 12).

The API contract (docs/plans/m1-mvp-core.md): `{question, mandate_id?}` in; `answer`,
`citations: [{record_id, title, kind, trust}]` (trust: Timur's answer 2 on #35, the
record's own column, "internal" or "untrusted"), `grounding: {figures, unmatched,
unknown_citations}`, `proposals` (empty until M1 PR 14), `usage: {input_tokens,
output_tokens}`, `cost_usd`, `stop` and `trace_id` out. JSON only, with
`Cache-Control: no-store` (app/http/security_headers.py sets it on every path but the
page). A request meets, in this order:

1. The login (401, current_advisor()) and, with the dev login, the shim's Host and
   Origin checks (400, 403), which run before any route (app/auth/dev_shim.py). The
   request's clock starts here, for the deadline below.
2. The content type (422): exactly one Content-Type of exactly application/json. It is
   checked before the rate limit counts, so a cross-site form (text/plain, which needs
   no preflight) cannot spend an advisor's asks (review finding sec-2).
3. The rate limit per verified advisor (429): at most REQUESTS_PER_WINDOW requests
   started in any WINDOW_SECONDS, and one in flight. In process memory, one entry per
   advisor that asked. A refused request is not counted; a malformed body is.
4. The body (422, one fixed body that echoes nothing): at most MAX_BODY bytes as they
   arrive, a JSON object of exactly `question` (1 to MAX_QUESTION characters, not
   blank) and an optional `mandate_id` of the loop's form. Without a mandate the ask
   covers all of the advisor's own mandates.
5. The model client, built from settings at the first ask and kept (ADR 0012): without
   a key, 503, kept until a restart (the settings are read once). The embedding model,
   loaded at the first ask and kept (about 1.5 s, once, on the event loop): without its
   files, or with other bytes than the pinned ones, 503, kept until a restart; any other
   failure to load (an I/O error, say) is 503 and tried again at the next ask. Nothing
   is built at startup, so the app serves /health without either.
6. The database (503): one advisor transaction opens and closes before the first model
   call, so a database that is down costs no model call.
7. The loop (app/harness/loop.py) with the READ handlers bound to the app's pool and
   embedder (read_handlers()). Each tool call opens and closes its own advisor
   transaction, so no connection is held while the model is called (PR 5b sec-5). The
   model client is wrapped in DeadlineClient: no model call starts DEADLINE_SECONDS or
   more after the request's clock started, and the request then ends with stop
   "budget", as when its tokens run out (Timur's answer 1 on #33: the loop itself reads
   no clock). This is no wall-clock bound: a call already running ends within the
   client's own timeouts (per phase, ADR 0012), and the tool calls of its turn still run,
   each statement without a timeout until M2 (PR 8's sec-4).
8. The citations' titles, kinds and trust, in one more advisor transaction
   (handlers.cited_records()), for the ids a tool returned and the answer cites. A trust
   outside ADR 0010's two is a GuardError there, so 500 like a row of another advisor.

A LoopError maps to STATUS by its kind, with one fixed body each: 502 for a failed model
call, 503 for the database, 500 for every other kind. A GuardError or a lost database in
the route's own transactions (steps 6 and 8) ends the request the same way, and the
route names the kind on the span as the loop does, so M2's alarm on a broken boundary
sees every one (review finding sec-1). The cost is an exact decimal string; trace_id is
the request span's, always set under create_app(). Nothing is logged here.

The rate limit and the lazy builds keep state between requests without a lock: they run
in async dependencies, on the event loop alone, with no await between a check and its
update. The route itself is sync, so FastAPI runs it in its thread pool and the blocking
pool, driver, embedder and model client never hold the event loop; the thread runs in a
copy of the request's context, so the loop's span attributes land on the request's span.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth.identity import VerifiedAdvisor, current_advisor
from app.config.settings import Settings
from app.db.session import GuardError, advisor_transaction
from app.embeddings.base import Embedder
from app.embeddings.model_files import ModelFileError
from app.harness import loop
from app.harness.context import BudgetExceeded
from app.harness.handlers import cited_records, read_handlers
from app.harness.model import ModelClient
from app.harness.model_client import ModelClientRefused, build_model_client
from app.observability import tracing

ROUTE = "/api/ask"
CONTENT_TYPE = "application/json"
# Bytes: the longest valid ask, every character escaped, characters outside the BMP as
# surrogate pairs of 12 bytes (Python's json.dumps), fits: about 24.8 KB (a test posts it).
MAX_BODY = 32_768
MAX_QUESTION = loop.MAX_QUESTION
MAX_MANDATE_ID = loop.MAX_MANDATE_ID
MANDATE_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"  # the loop's form (app/harness/loop.py)
# Each citation's fields, in the contract's order (the plan's amendment names them).
CITATION_FIELDS = ("record_id", "title", "kind", "trust")
# Per advisor: requests started in any window, and one in flight.
REQUESTS_PER_WINDOW = 10
WINDOW_SECONDS = 60.0
# No model call starts this long after the route began (the loop reads no clock).
DEADLINE_SECONDS = 90.0

# The fixed bodies: each names the reason, never a value, a variable or a file.
INVALID = "invalid request"
TOO_MANY = "too many requests"
NO_MODEL = "model unavailable"
NO_SEARCH = "search unavailable"
NO_DATABASE = "database unavailable"
MODEL_FAILED = "the model call failed"
FAILED = "the request failed"

# A LoopError's kind -> its status and body.
STATUS: Mapping[str, tuple[int, str]] = MappingProxyType(
    {
        "model_call": (502, MODEL_FAILED),
        "database": (503, NO_DATABASE),
        **{kind: (500, FAILED) for kind in loop.KINDS - {"model_call", "database"}},
    }
)


class AskRequest(BaseModel):
    """The body of an ask; strict, so a number is not a question."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    mandate_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_MANDATE_ID, pattern=MANDATE_ID
    )


class EmbedderUnavailable(Exception):
    """The model files are missing or not the pinned bytes, or the graph is refused."""


def build_embedder() -> Embedder:
    """The real embedder (ADR 0005), from the checked model files; EmbedderUnavailable
    for files that are missing or not the pinned bytes, or a refused graph."""
    # Imported here, so the model runtime loads at the first ask, not when the app starts.
    from app.embeddings.onnx import EmbedderError, OnnxEmbedder

    try:
        embedder: Embedder | None = OnnxEmbedder()
    except (ModelFileError, EmbedderError):
        embedder = None
    if embedder is None:
        # Raised outside the except, so the file error is neither cause nor context.
        raise EmbedderUnavailable("the embedding model is not in place")
    return embedder


class RateLimit:
    """Per advisor id: at most per_window requests started in any window_seconds, and
    one in flight. A refused request is not counted. Called on the event loop only."""

    def __init__(self, per_window: int, window_seconds: float, clock: Callable[[], float]) -> None:
        self._per_window = per_window
        self._window = window_seconds
        self._clock = clock
        self._starts: dict[str, deque[float]] = {}
        self._busy: set[str] = set()

    def admit(self, advisor_id: str) -> bool:
        if advisor_id in self._busy:
            return False
        now = self._clock()
        starts = self._starts.setdefault(advisor_id, deque())
        while starts and now - starts[0] >= self._window:
            starts.popleft()
        if len(starts) >= self._per_window:
            return False
        starts.append(now)
        self._busy.add(advisor_id)
        return True

    def release(self, advisor_id: str) -> None:
        self._busy.discard(advisor_id)


class DeadlineClient:
    """One request's model client: a call that would start at or after ends_at raises
    BudgetExceeded instead, so the loop ends the turn with stop "budget"."""

    __slots__ = ("_clock", "_ends_at", "_inner", "model")

    def __init__(self, inner: ModelClient, ends_at: float, clock: Callable[[], float]) -> None:
        self._inner = inner
        self._ends_at = ends_at
        self._clock = clock
        self.model = inner.model

    def create(self, **request: Any) -> Mapping[str, Any]:
        if self._clock() >= self._ends_at:
            raise BudgetExceeded("the request's time is spent")
        return self._inner.create(**request)


class AskState:
    """What the route keeps between requests: the rate limit and the lazily built model
    client and embedder. Only the event loop touches it, so it holds no lock."""

    def __init__(
        self,
        settings: Settings,
        model_client: ModelClient | None,
        embedder: Embedder | None,
        clock: Callable[[], float],
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.limit = RateLimit(REQUESTS_PER_WINDOW, WINDOW_SECONDS, clock)
        self._model_client = model_client
        self._embedder = embedder
        self._no_model_client = False
        self._no_embedder = False

    def model_client(self) -> ModelClient | None:
        if self._model_client is None and not self._no_model_client:
            try:
                self._model_client = build_model_client(self.settings)
            except ModelClientRefused:
                self._no_model_client = True  # no key: a restart reads .env again
        return self._model_client

    def embedder(self) -> Embedder | None:
        if self._embedder is not None or self._no_embedder:
            return self._embedder
        try:
            self._embedder = build_embedder()
        except EmbedderUnavailable:
            self._no_embedder = True  # the files are read once: a restart reads them again
        except Exception:  # noqa: BLE001 - another failure (an I/O error, say) is tried again
            return None
        return self._embedder


def _state(request: Request) -> AskState:
    return request.app.state.ask


async def admitted(
    request: Request, advisor: Annotated[VerifiedAdvisor, Depends(current_advisor)]
) -> AsyncIterator[VerifiedAdvisor]:
    """The login's advisor, with a JSON body (422 otherwise) and within the rate limit
    (429 otherwise), until the route ends. The request's clock starts here."""
    state = _state(request)
    request.state.ask_began = state.clock()
    if request.headers.getlist("content-type") != [CONTENT_TYPE]:
        raise _invalid()  # before the limit counts (review finding sec-2)
    limit = state.limit
    if not limit.admit(advisor.advisor_id):
        raise HTTPException(status_code=429, detail=TOO_MANY)
    try:
        yield advisor
    finally:
        limit.release(advisor.advisor_id)


def _invalid() -> HTTPException:
    return HTTPException(status_code=422, detail=INVALID)


async def ask_body(request: Request) -> AskRequest:
    """The body as an AskRequest, or one fixed 422 (the content type is admitted()'s)."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise _invalid()
    ask: AskRequest | None
    try:
        ask = AskRequest.model_validate_json(body)
    except ValidationError:
        ask = None
    # Raised outside the except: pydantic's errors quote the input (review finding sec-7).
    if ask is None or not ask.question.strip():
        raise _invalid()
    return ask


async def services(request: Request) -> tuple[ModelClient, Embedder]:
    """The model client and the embedder, built at the first ask (503 without them)."""
    state = _state(request)
    client = state.model_client()
    if client is None:
        raise HTTPException(status_code=503, detail=NO_MODEL)
    embedder = state.embedder()
    if embedder is None:
        raise HTTPException(status_code=503, detail=NO_SEARCH)
    return client, embedder


def _failed(kind: str) -> HTTPException:
    """The fixed answer for a LoopError's kind, named on the request's span as the loop
    names its own (a reviewed word: tracing.LOOP's error key)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(tracing.LOOP["error"], kind)
    status, detail = STATUS[kind]
    return HTTPException(status_code=status, detail=detail)


# A sync route: FastAPI runs it in its thread pool (the module docstring says why).
def post_ask(
    request: Request,
    advisor: Annotated[VerifiedAdvisor, Depends(admitted, scope="function")],
    ask: Annotated[AskRequest, Depends(ask_body)],
    found: Annotated[tuple[ModelClient, Embedder], Depends(services)],
) -> JSONResponse:
    state = _state(request)
    began: float = request.state.ask_began
    pool = request.app.state.pool
    client, embedder = found
    failure: str | None = None
    try:
        with advisor_transaction(pool, advisor) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError:  # PoolTimeout and PoolClosed too
        failure = "database"
    except GuardError:
        failure = "guard"
    if failure is not None:
        raise _failed(failure)
    try:
        result = loop.run(
            advisor,
            ask.question,
            ask.mandate_id,
            client=DeadlineClient(client, began + DEADLINE_SECONDS, state.clock),
            handlers=read_handlers(pool, embedder),
            model=client.model,
        )
    except loop.LoopError as error:
        failure = error.kind
    if failure is not None:
        raise _failed(failure)
    try:
        citations = cited_records(pool, advisor, result.citations)
    except psycopg.OperationalError:
        failure = "database"
    except GuardError:
        failure = "guard"
    if failure is not None:
        raise _failed(failure)
    report = result.grounding
    return JSONResponse(
        {
            "answer": result.answer,
            "citations": [
                {field: getattr(item, field) for field in CITATION_FIELDS} for item in citations
            ],
            "grounding": {
                "figures": report.figures,
                "unmatched": report.unmatched,
                "unknown_citations": report.unknown_citations,
            },
            "proposals": [],
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
            # Exact, in plain notation (never 1E-6): a JSON number would be a float.
            "cost_usd": format(result.cost_usd, "f"),
            "stop": result.stop,
            "trace_id": result.trace_id,
        }
    )


def mount(
    app: FastAPI,
    settings: Settings,
    *,
    model_client: ModelClient | None = None,
    embedder: Embedder | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Adds the route to the app itself (not an included router, so the route list and
    the no-redirect scan see it). model_client, embedder and clock are for tests and the
    live runner (M1 PR 16); the app builds the first two at the first ask."""
    app.state.ask = AskState(settings, model_client, embedder, clock or time.monotonic)
    app.add_api_route(ROUTE, post_ask, methods=["POST"], response_class=JSONResponse)
