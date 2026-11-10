"""The READ handlers' logic, without a database (M1 PR 8; ADR 0013).

tests/db/test_retrieval.py runs them on the corpus; this file pins what needs no rows:
- reciprocal rank fusion: each list adds 1/(k + rank) once per item, ties go by key, k
  is the ADR's;
- the ranked lists: best first by score or distance, each item once at its best, at most
  CANDIDATES, ties by key;
- the handler map holds exactly the registry's READ tools, and cannot be changed;
- a call is checked before any work: another type of advisor or of arguments raises
  before the embedder or the pool is touched; a query over the model's limit is refused
  with a fixed kind, before any connection is taken, with no input in the error;
- a row of another advisor, which row-level security should never let through, makes
  the handler refuse instead of returning it (a second line, behind the policies);
- the statements: each list keeps to CANDIDATES rows, the vector ones compare vectors of
  the query's model only, and the portfolio comes from the portfolio system only.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import MappingProxyType
from typing import Any

import pytest

from app.auth.identity import issue
from app.config import branding
from app.db import queries
from app.db.session import GuardError
from app.embeddings import base, onnx
from app.harness import handlers
from app.harness.mvp_tools import GetPortfolio, SearchClientDocs, build_registry
from app.harness.tools import Effect

MINE = branding.DEMO_MANDATE.advisor_id
OTHER = next(person.id for person in branding.ADVISORS if person.id != MINE)
VECTOR = "[1.0]"


class SpyEmbedder:
    model_id = "fake/spy"
    revision = "spy"
    dim = 384

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.error is not None:
            raise self.error
        return [1.0] + [0.0] * (self.dim - 1)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("no passages in a search")


class Untouchable:
    """A pool that fails the test if anything asks it for a connection."""

    def __init__(self) -> None:
        self.asked = 0

    def connection(self, *args: object, **kwargs: object) -> Any:
        self.asked += 1
        raise AssertionError("the pool was asked for a connection")


class FakeRows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    """Answers each statement from a table of canned rows; records what ran."""

    def __init__(self, answers: dict[str, list[tuple[Any, ...]]]) -> None:
        self.answers = answers
        self.ran: list[str] = []

    def execute(self, statement: str, params: object = None) -> FakeRows:
        self.ran.append(statement)
        return FakeRows(self.answers.get(statement, []))


# Reciprocal rank fusion.


def test_rrf_k_is_the_usual_constant() -> None:
    assert handlers.RRF_K == 60


def test_fuse_adds_one_over_k_plus_rank_per_list() -> None:
    # a: 1/61 + 1/62; c: 1/63 + 1/61; b: 1/62.
    assert handlers.fuse([[("d", "a"), ("d", "b"), ("d", "c")], [("d", "c"), ("d", "a")]]) == [
        ("d", "a"),
        ("d", "c"),
        ("d", "b"),
    ]


def test_fuse_counts_an_item_once_per_list_at_its_best_rank() -> None:
    # Counted once: y (1/62 + 1/62) beats x (1/61); counted twice, x would win.
    x, y, z = ("d", "x"), ("d", "y"), ("d", "z")
    assert handlers.fuse([[x, x, y], [z, y]]) == [y, x, z]


def test_fuse_breaks_ties_by_kind_then_id() -> None:
    first = [("product_sheet", "a")]
    second = [("document", "b")]
    assert handlers.fuse([first, second]) == [("document", "b"), ("product_sheet", "a")]


def test_fuse_uses_k() -> None:
    a, b, x, y, z = (("d", name) for name in "abxyz")
    lists = [[a, x, b], [y, z, b]]
    # k = 0: a and y (1/1) beat b (1/3 + 1/3); k = 60: b (2/63) beats a and y (1/61).
    assert handlers.fuse(lists, k=0)[:2] == [a, y]
    assert handlers.fuse(lists)[0] == b


def test_fuse_of_nothing_is_nothing() -> None:
    assert handlers.fuse([]) == [] and handlers.fuse([[], []]) == []


# The ranked lists.


def test_ranked_by_score_puts_the_highest_first() -> None:
    rows = [(("d", "1"), 0.1), (("d", "3"), 0.3), (("d", "2"), 0.3)]
    assert handlers.ranked(rows, best="high") == [("d", "2"), ("d", "3"), ("d", "1")]


def test_ranked_by_distance_puts_the_nearest_first_each_once() -> None:
    rows = [(("d", "a"), 0.5), (("d", "b"), 0.3), (("d", "a"), 0.1)]
    assert handlers.ranked(rows, best="low") == [("d", "a"), ("d", "b")]


def test_ranked_keeps_candidates() -> None:
    rows = [(("d", f"{n:02d}"), float(n)) for n in range(handlers.CANDIDATES + 5)]
    assert len(handlers.ranked(rows, best="low")) == handlers.CANDIDATES == 20


def test_ranked_takes_high_or_low() -> None:
    with pytest.raises(ValueError, match="high or low"):
        handlers.ranked([], best="up")


def test_vector_text_is_pgvectors_text_form() -> None:
    assert handlers.vector_text([1, 0.5, -0.25]) == "[1.0,0.5,-0.25]"


# The handler map.


def test_the_map_holds_the_registry_read_tools() -> None:
    registry = build_registry()
    read = {name for name in registry.names() if registry.spec(name).effect is Effect.READ}
    found = handlers.read_handlers(Untouchable(), SpyEmbedder())  # type: ignore[arg-type]
    assert set(found) == read == {handlers.SEARCH, handlers.PORTFOLIO}
    assert isinstance(found, MappingProxyType)
    with pytest.raises(TypeError):
        found["draft_email"] = found[handlers.SEARCH]  # type: ignore[index]


# A call is checked before any work.


@pytest.mark.parametrize(
    ("advisor", "arguments"),
    [
        (MINE, SearchClientDocs(query="x")),
        (None, SearchClientDocs(query="x")),
        (issue(MINE), GetPortfolio(mandate_id="m")),
        (issue(MINE), {"query": "x"}),
    ],
    ids=["advisor-str", "advisor-none", "other-arguments", "dict"],
)
def test_search_checks_its_call_before_any_work(advisor: object, arguments: object) -> None:
    pool, embedder = Untouchable(), SpyEmbedder()
    with pytest.raises(TypeError):
        handlers.search_client_docs(pool, embedder, advisor, arguments)  # type: ignore[arg-type]
    assert embedder.queries == [] and pool.asked == 0


@pytest.mark.parametrize(
    ("advisor", "arguments"),
    [
        (MINE, GetPortfolio(mandate_id="m")),
        (issue(MINE), SearchClientDocs(query="x")),
        (issue(MINE), {"mandate_id": "m"}),
    ],
    ids=["advisor-str", "other-arguments", "dict"],
)
def test_portfolio_checks_its_call_before_any_work(advisor: object, arguments: object) -> None:
    pool = Untouchable()
    with pytest.raises(TypeError):
        handlers.get_portfolio(pool, advisor, arguments)  # type: ignore[arg-type]
    assert pool.asked == 0


def test_text_too_long_is_one_class_for_every_embedder() -> None:
    assert onnx.TextTooLong is base.TextTooLong
    assert issubclass(base.TextTooLong, ValueError)


def test_a_query_over_the_models_limit_is_refused_before_any_connection() -> None:
    canary = "cnry-too-long-query"
    pool = Untouchable()
    embedder = SpyEmbedder(base.TextTooLong(f"600 tokens; the model takes at most 512 {canary}"))
    with pytest.raises(handlers.ToolFailed) as refused:
        handlers.search_client_docs(pool, embedder, issue(MINE), SearchClientDocs(query=canary))
    assert refused.value.kind == handlers.QUERY_TOO_LONG == "query_too_long"
    assert str(refused.value) == "query_too_long" and refused.value.args == ("query_too_long",)
    # No chained exception carries the embedder's message along.
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert pool.asked == 0


# A row of another advisor.


def test_search_refuses_a_row_of_another_advisor() -> None:
    conn = FakeConnection({queries.SEARCH_DOCUMENTS_ENGLISH: [(OTHER, "theirs", 0.9)]})
    with pytest.raises(GuardError, match="another advisor"):
        handlers.search(conn, MINE, SearchClientDocs(query="x"), VECTOR, "m", "r")


def test_the_vector_branch_refuses_a_row_of_another_advisor() -> None:
    conn = FakeConnection({queries.NEAREST_CHUNKS: [(OTHER, "theirs", 0.1)]})
    with pytest.raises(GuardError, match="another advisor"):
        handlers.nearest_chunks(conn, MINE, VECTOR, "m", "r")


def test_the_portfolio_refuses_a_row_of_another_advisor() -> None:
    conn = FakeConnection(
        {
            queries.MANDATE_VISIBLE: [(MINE, "m")],
            queries.PORTFOLIO_REPORT: [(OTHER, "theirs", "m", "t", "english", "b")],
        }
    )
    with pytest.raises(GuardError, match="another advisor"):
        handlers.portfolio(conn, MINE, "m")
    conn = FakeConnection({queries.MANDATE_VISIBLE: [(OTHER, "m")]})
    with pytest.raises(GuardError, match="another advisor"):
        handlers.portfolio(conn, MINE, "m")


def test_an_unseen_mandate_runs_no_search() -> None:
    conn = FakeConnection({})
    assert (
        handlers.search(conn, MINE, SearchClientDocs(query="x", mandate_id="m"), VECTOR, "m", "r")
        == []
    )
    assert conn.ran == [queries.MANDATE_VISIBLE]
    conn = FakeConnection({})
    assert handlers.portfolio(conn, MINE, "m") is None
    assert conn.ran == [queries.MANDATE_VISIBLE]


def test_a_mandate_search_leaves_the_sheets_out() -> None:
    conn = FakeConnection({queries.MANDATE_VISIBLE: [(MINE, "m")]})
    handlers.search(conn, MINE, SearchClientDocs(query="x", mandate_id="m"), VECTOR, "m", "r")
    assert not any("product_sheets" in statement for statement in conn.ran), conn.ran
    conn = FakeConnection({})
    handlers.search(conn, MINE, SearchClientDocs(query="x"), VECTOR, "m", "r")
    assert sum("product_sheets" in statement for statement in conn.ran) >= 3, conn.ran


# The statements.

LISTS = (
    "SEARCH_DOCUMENTS_ENGLISH",
    "SEARCH_DOCUMENTS_GERMAN",
    "SEARCH_MANDATE_DOCUMENTS_ENGLISH",
    "SEARCH_MANDATE_DOCUMENTS_GERMAN",
    "SEARCH_SHEETS_ENGLISH",
    "SEARCH_SHEETS_GERMAN",
    "NEAREST_CHUNKS",
    "NEAREST_MANDATE_CHUNKS",
    "NEAREST_SHEETS",
)


@pytest.mark.parametrize("name", LISTS)
def test_each_list_keeps_to_its_candidates(name: str) -> None:
    assert f" LIMIT {handlers.CANDIDATES}" in getattr(queries, name)


@pytest.mark.parametrize("name", [name for name in LISTS if name.startswith("NEAREST_")])
def test_the_vector_lists_compare_one_model(name: str) -> None:
    statement = getattr(queries, name)
    assert "embedding_model = %s" in statement and "embedding_revision = %s" in statement
    # relaxed_order may return rows a little out of order: they are sorted again.
    assert "MATERIALIZED" in statement or "chunks" not in statement


def test_the_portfolio_comes_from_the_portfolio_system() -> None:
    assert "d.origin = 'portfolio_system'" in queries.PORTFOLIO_REPORT
