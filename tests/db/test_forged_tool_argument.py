"""A forged tool argument goes nowhere (plan PR 6, M1 PR 8; threat-model path 2; CLAUDE.md
rule 8).

The model's arguments to search_client_docs go through the real registry to the real
handler (app/harness/handlers.py), which embeds the query with the real model and
searches in its own advisor transaction. Two claims:

- An argument that names an identity (plain, camelCase, nested, in a list, fullwidth,
  German, or a Cyrillic homoglyph) gets a structured error, and nothing runs: neither
  the handler nor the embedder is called, and the pool hands out no connection.
- PR 5b's forged payloads pass validation, because they are plain query text, and the
  handler returns only the logged-in advisor's records: their token document in the
  payload's language among them, nothing of the other advisor's. The boundary is the
  database's, not the validator's.

The advisor comes from issue(), as the login will provide it; nothing in the arguments
can change it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from dbtools import SUPERUSER_URL
from forged_payloads import PAYLOAD_IDS, PAYLOADS, WORD, token_document_id
from psycopg import sql
from psycopg_pool import ConnectionPool
from seed_rows import MINE, OTHER

from app.auth.identity import VerifiedAdvisor, issue
from app.db import names
from app.db.connect import connect
from app.embeddings.onnx import OnnxEmbedder
from app.harness.context import Untrusted
from app.harness.handlers import read_handlers
from app.harness.mvp_tools import build_registry
from app.harness.tools import RejectedCall, ToolRegistry, ValidCall

pytestmark = pytest.mark.usefixtures("database", "seeded", "token_documents")

TOOL = "search_client_docs"


class CountingEmbedder:
    """The real embedder, counting the queries it embeds."""

    def __init__(self, embedder: OnnxEmbedder) -> None:
        self.embedder = embedder
        self.model_id = embedder.model_id
        self.revision = embedder.revision
        self.dim = embedder.dim
        self.queries = 0

    def embed_query(self, text: str) -> list[float]:
        self.queries += 1
        return self.embedder.embed_query(text)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("a search embeds no passages")


class HandlerSpy:
    """The real handler, recording each call it gets."""

    def __init__(self, pool: ConnectionPool, embedder: CountingEmbedder) -> None:
        self.pool = pool
        self.embedder = embedder
        self.handler = read_handlers(pool, embedder)[TOOL]  # type: ignore[arg-type]
        self.calls: list[Any] = []

    def __call__(self, advisor: VerifiedAdvisor, arguments: Any) -> Untrusted[Any]:
        self.calls.append(arguments)
        return self.handler(advisor, arguments)


def dispatch(
    registry: ToolRegistry, spy: HandlerSpy, advisor: VerifiedAdvisor, arguments: object
) -> RejectedCall | Untrusted[Any]:
    """What M1's loop does with a tool_use block: validate, and run only a valid call."""
    outcome = registry.validate_call(TOOL, arguments)
    if isinstance(outcome, RejectedCall):
        return outcome
    assert isinstance(outcome, ValidCall)
    return spy(advisor, outcome.arguments)


def requests(pool: ConnectionPool) -> int:
    return pool.get_stats().get("requests_num", 0)


@pytest.fixture
def registry() -> ToolRegistry:
    return build_registry()


@pytest.fixture
def spy(app_pool: ConnectionPool, embedder: OnnxEmbedder) -> HandlerSpy:
    return HandlerSpy(app_pool, CountingEmbedder(embedder))


@pytest.fixture(scope="module")
def owners(token_documents: None) -> Iterator[dict[str, str]]:
    """Every record id the superuser sees, and whose it is (sheets: the empty string)."""
    with connect(SUPERUSER_URL) as conn:
        documents = conn.execute(
            sql.SQL("SELECT id, advisor_id FROM {}").format(
                sql.Identifier(names.SCHEMA, "documents")
            )
        ).fetchall()
        sheets = conn.execute(
            sql.SQL("SELECT id, '' FROM {}").format(sql.Identifier(names.SCHEMA, "product_sheets"))
        ).fetchall()
    yield dict(documents + sheets)


def foreign(ids: Sequence[str], owners: dict[str, str], person: object) -> list[str]:
    """The ids that are neither person's documents nor product sheets."""
    return [record for record in ids if owners.get(record) not in ("", person.id)]  # type: ignore[attr-defined]


def test_positive_control_a_plain_call_runs(
    registry: ToolRegistry, spy: HandlerSpy, owners: dict[str, str]
) -> None:
    before = requests(spy.pool)
    result = dispatch(registry, spy, issue(OTHER.id), {"query": WORD})
    assert not isinstance(result, RejectedCall), result
    ids = result.provenance.record_ids
    assert {token_document_id(OTHER.id, lang) for lang in ("english", "german")} <= set(ids), ids
    assert foreign(ids, owners, OTHER) == []
    assert len(spy.calls) == 1 and spy.embedder.queries == 1
    assert requests(spy.pool) > before


FORGED = {
    "plain": {"query": WORD, "advisor_id": "{other}"},
    "camel": {"query": WORD, "advisorId": "{other}"},
    "nested": {"query": WORD, "filters": {"advisor_id": "{other}"}},
    "in-list": {"query": WORD, "filters": [{"owner": "{other}"}]},
    "fullwidth": {"query": WORD, "ａｄｖｉｓｏｒ_id": "{other}"},
    "german": {"query": WORD, "berater_id": "{other}"},
    "mitarbeiter": {"query": WORD, "mitarbeiter_id": "{other}"},
    "on-behalf": {"query": WORD, "onBehalfOf": "{other}"},
    "user": {"query": WORD, "user": "{other}"},
    "homoglyph": {"query": WORD, "аdvisor_id": "{other}"},  # Cyrillic a
}


def forged(name: str) -> dict[str, Any]:
    def fill(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return OTHER.id if value == "{other}" else value

    return fill(FORGED[name])


@pytest.mark.parametrize("name", list(FORGED))
def test_identity_arguments_are_rejected_before_any_sql(
    name: str, registry: ToolRegistry, spy: HandlerSpy
) -> None:
    before = requests(spy.pool)
    result = dispatch(registry, spy, issue(MINE.id), forged(name))
    assert isinstance(result, RejectedCall), f"{name}: the call ran and returned {result}"
    assert result.tool == TOOL and result.errors, result
    kinds = {error.type for error in result.errors}
    expected = "extra_forbidden" if name == "homoglyph" else "identity_argument"
    assert expected in kinds, f"{name}: {result.errors}"
    assert OTHER.id not in repr(result.errors), "the error echoes the forged value"
    assert spy.calls == [], f"{name}: the handler ran"
    assert spy.embedder.queries == 0, f"{name}: the query was embedded"
    assert requests(spy.pool) == before, f"{name}: the pool handed out a connection"


@pytest.mark.parametrize(("lang", "payload"), PAYLOADS, ids=PAYLOAD_IDS)
def test_forged_payloads_pass_validation_and_stay_inert(
    lang: str, payload: str, registry: ToolRegistry, spy: HandlerSpy, owners: dict[str, str]
) -> None:
    outcome = registry.validate_call(TOOL, {"query": payload})
    assert isinstance(outcome, ValidCall), f"plain query text was rejected: {outcome}"
    result = dispatch(registry, spy, issue(MINE.id), {"query": payload})
    assert not isinstance(result, RejectedCall)
    ids = result.provenance.record_ids
    leaked = foreign(ids, owners, MINE)
    assert not leaked, f"{OTHER.name} records reached {MINE.name}: {leaked}"
    assert token_document_id(MINE.id, lang) in ids, ids
    # The same handler right after, for the same advisor: the payload moved nothing.
    follow_up = dispatch(registry, spy, issue(MINE.id), {"query": WORD})
    assert not isinstance(follow_up, RejectedCall)
    assert foreign(follow_up.provenance.record_ids, owners, MINE) == []
    assert token_document_id(MINE.id, lang) in follow_up.provenance.record_ids


@pytest.mark.parametrize("tool", ["search_client_docs", "get_portfolio"])
@pytest.mark.parametrize(("lang", "payload"), PAYLOADS, ids=PAYLOAD_IDS)
def test_forged_payloads_as_a_mandate_id_name_no_mandate(
    lang: str,
    payload: str,
    tool: str,
    registry: ToolRegistry,
    app_pool: ConnectionPool,
    embedder: OnnxEmbedder,
) -> None:
    # Security review sec-6: the payloads through the other text argument, which reaches
    # the mandate lookup and the mandate's statements, and through get_portfolio.
    arguments = {"mandate_id": payload} | ({"query": WORD} if tool == TOOL else {})
    outcome = registry.validate_call(tool, arguments)
    assert isinstance(outcome, ValidCall), f"{tool} rejected the payload: {outcome}"
    result = read_handlers(app_pool, embedder)[tool](issue(MINE.id), outcome.arguments)
    assert result.provenance.record_ids == (), result.provenance
