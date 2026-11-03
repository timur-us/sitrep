"""A forged tool argument goes nowhere (plan PR 6; threat-model path 2; CLAUDE.md rule 8).

The model's arguments to search_client_docs go through the real registry; the handler
is a test-local stand-in that runs the real search statements in the advisor transaction
(M1 retargets this to the real handler). Two claims:

- An argument that names an identity (plain, camelCase, nested, in a list, fullwidth,
  German, or a Cyrillic homoglyph) gets a structured error, and nothing runs: the
  handler is not called and the pool hands out no connection.
- PR 5b's forged payloads pass validation, because they are plain query text, and
  still return only the logged-in advisor's token documents, with the advisor setting
  unchanged: the boundary is the database's, not the validator's.

The advisor comes from issue(), as the login will provide it; nothing in the arguments
can change it.
"""

from __future__ import annotations

from typing import Any

import pytest
from forged_payloads import PAYLOAD_IDS, PAYLOADS, SEARCH, SETTING, WORD, own
from psycopg_pool import ConnectionPool
from seed_rows import MINE, OTHER

from app.auth.identity import VerifiedAdvisor, issue
from app.db.session import advisor_transaction
from app.harness.mvp_tools import SearchClientDocs, build_registry
from app.harness.tools import RejectedCall, ToolRegistry, ValidCall

pytestmark = pytest.mark.usefixtures("database", "seeded", "token_documents")

TOOL = "search_client_docs"


class SearchSpy:
    """The test-local handler: records each call, then searches both languages."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.calls: list[SearchClientDocs] = []

    def __call__(self, advisor: VerifiedAdvisor, arguments: SearchClientDocs) -> dict[str, Any]:
        self.calls.append(arguments)
        with advisor_transaction(self.pool, advisor) as conn:
            found = []
            for statement in SEARCH.values():
                found += conn.execute(statement, (arguments.query,)).fetchall()
            setting = conn.execute(SETTING).fetchone()[0]
            follow_up = conn.execute(SEARCH["english"], (WORD,)).fetchall()
        return {"found": sorted(found), "setting": setting, "follow_up": follow_up}


def dispatch(
    registry: ToolRegistry, spy: SearchSpy, advisor: VerifiedAdvisor, arguments: object
) -> RejectedCall | dict[str, Any]:
    """What M1's loop does with a tool_use block: validate, and run only a valid call."""
    outcome = registry.validate_call(TOOL, arguments)
    if isinstance(outcome, RejectedCall):
        return outcome
    assert isinstance(outcome, ValidCall)
    return spy(advisor, outcome.arguments)  # type: ignore[arg-type]


def requests(pool: ConnectionPool) -> int:
    return pool.get_stats().get("requests_num", 0)


@pytest.fixture
def registry() -> ToolRegistry:
    return build_registry()


@pytest.fixture
def spy(app_pool: ConnectionPool) -> SearchSpy:
    return SearchSpy(app_pool)


def mine_everywhere() -> list[tuple[str, str]]:
    return sorted(own(MINE, "english") + own(MINE, "german"))


def test_positive_control_a_plain_call_runs(registry: ToolRegistry, spy: SearchSpy) -> None:
    before = requests(spy.pool)
    result = dispatch(registry, spy, issue(OTHER.id), {"query": WORD})
    assert not isinstance(result, RejectedCall), result
    assert result["found"] == sorted(own(OTHER, "english") + own(OTHER, "german"))
    assert result["setting"] == OTHER.id
    assert len(spy.calls) == 1 and requests(spy.pool) > before


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
    "homoglyph": {"query": WORD, "\u0430dvisor_id": "{other}"},  # Cyrillic a
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
    name: str, registry: ToolRegistry, spy: SearchSpy
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
    assert requests(spy.pool) == before, f"{name}: the pool handed out a connection"


@pytest.mark.parametrize(("lang", "payload"), PAYLOADS, ids=PAYLOAD_IDS)
def test_forged_payloads_pass_validation_and_stay_inert(
    lang: str, payload: str, registry: ToolRegistry, spy: SearchSpy
) -> None:
    outcome = registry.validate_call(TOOL, {"query": payload})
    assert isinstance(outcome, ValidCall), f"plain query text was rejected: {outcome}"
    result = dispatch(registry, spy, issue(MINE.id), {"query": payload})
    assert not isinstance(result, RejectedCall)
    leaked = [row for row in result["found"] + result["follow_up"] if row[0] != MINE.id]
    assert not leaked, f"{OTHER.name} rows visible under {MINE.name}: {leaked}"
    assert own(MINE, lang)[0] in result["found"], result["found"]
    assert set(result["found"]) <= set(mine_everywhere()), result["found"]
    assert result["setting"] == MINE.id, f"the setting moved to {result['setting']!r}"
    assert result["follow_up"] == own(MINE, "english"), result["follow_up"]
