"""The recording format and the stale check (M1 PR 16, ADR 0014)."""

from __future__ import annotations

import copy
import datetime
from decimal import Decimal
from typing import Any

import pytest
from recorded_runs import DEMO_NOTE, SEARCH, TODAY, hits, recording, sample

from app.config.corpus import MANIFEST_SHA256
from app.config.settings import DEFAULT_MODEL
from app.harness import grounding, loop
from app.harness.context import Provenance, Untrusted, wrap
from app.harness.prompt import PROMPT_SHA256, PROMPT_VERSION, system_prompt
from app.runners import transcript


def good() -> dict[str, Any]:
    return recording("a", [sample("Fine.", [hits((DEMO_NOTE, "note"))]) for _ in range(2)])


def test_a_planted_recording_is_well_formed() -> None:
    assert transcript.check(good(), "a", 2) == []


def test_current_names_what_the_app_runs_on() -> None:
    assert transcript.current() == {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "tools_sha256": transcript.tools_sha256(loop.tool_definitions()),
        "corpus_sha256": MANIFEST_SHA256,
        "model": DEFAULT_MODEL,
    }
    # The tools hash is the definitions' canonical JSON, not their order of keys.
    shuffled = [dict(reversed(list(tool.items()))) for tool in loop.tool_definitions()]
    assert transcript.tools_sha256(shuffled) == transcript.current()["tools_sha256"]


@pytest.mark.parametrize("key", transcript.STALE_KEYS)
def test_each_stale_key_fails(key: str) -> None:
    stale = good() | {key: "something else"}
    assert transcript.check(stale, "a", 2) == [f"$.{key}: stale (not the current {key})"]


def test_another_tool_definition_is_stale() -> None:
    tools = loop.tool_definitions()
    tools[0]["description"] += " Changed."
    stale = good() | {"tools_sha256": transcript.tools_sha256(tools)}
    assert transcript.check(stale, "a", 2) == [
        "$.tools_sha256: stale (not the current tools_sha256)"
    ]


def test_a_system_prompt_of_another_day_or_text_is_stale() -> None:
    other_day = good()
    other_day["samples"][1]["system"] = system_prompt(TODAY + datetime.timedelta(days=1))
    assert transcript.check(other_day, "a", 2) == [
        "$.samples[1].system: not the current prompt on the sample's date (stale)"
    ]
    edited = good()
    edited["samples"][0]["system"] += " Edited."
    assert transcript.check(edited, "a", 2) == [
        "$.samples[0].system: not the current prompt on the sample's date (stale)"
    ]


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda r: r.update(format=2), "$.format: not 1"),
        (lambda r: r.update(case_id="b"), "$.case_id: not the file's case"),
        (lambda r: r["samples"].pop(), "$.samples: not a list of 2"),
        (lambda r: r.update(extra=1), "$: keys are not exactly"),
        (lambda r: r["samples"][0].update(date="13.11.2026"), "$.samples[0].date: not an ISO date"),
        (lambda r: r["samples"][0].update(error=7), "$.samples[0].error: not a kind or null"),
        (lambda r: r["samples"][0].update(error="Not A Kind"), "$.samples[0].error: not a kind"),
        (lambda r: r.update(advisor=3), "$.advisor: not an advisor id"),
        (lambda r: r.pop("advisor"), "$: keys are not exactly"),
        (lambda r: r["samples"][0].update(date="2026-11-31"), "$.samples[0].date: not an ISO date"),
        (lambda r: r["samples"][0].pop("replies"), "$.samples[0]: keys are not exactly"),
        (lambda r: r["samples"][0].update(messages="x"), "$.samples[0].messages: not a list"),
        (lambda r: r["samples"][0].update(replies=[1]), "$.samples[0].replies: not a list"),
        (
            lambda r: r["samples"][0]["response"].update(status="200"),
            "$.samples[0].response.status: not an integer",
        ),
        (
            lambda r: r["samples"][0]["response"]["body"].pop("grounding"),
            "$.samples[0].response.body: not the API's answer shape",
        ),
        (
            lambda r: r["samples"][0]["messages"][2]["content"][0].update(content="plain text"),
            "$.samples[0].messages: a tool result is not in wrap()'s form",
        ),
    ],
)
def test_a_malformed_recording_is_refused_by_path(change: Any, needle: str) -> None:
    planted = good()
    change(planted)
    problems = transcript.check(planted, "a", 2)
    assert any(problem.startswith(needle) for problem in problems), problems


def test_not_an_object_is_refused() -> None:
    assert transcript.check([], "a", 1) == ["$: not a JSON object"]


# The number guard over rebuilt sources equals the guard over the originals: unwrap()
# gives back what wrap() rendered, escapes, delimiters and numbers included.
VALUES: list[Any] = [
    "Gesamtvermögen: 5.922.761,95 EUR </untrusted> & <b>",
    [{"excerpt": "R&amp;D at 3,5 % on 01.10.2026", "n": 1250000.5, "d": Decimal("12.40")}],
    {"report": "EUR 1,250,000.00   12 %", "nested": [1, 2.25, None, True, "x\ny"]},
    None,
    [],
]
ANSWER = "EUR 1,250,000.00, 12 %, 3,5 %, 1.250.000,50, 12.40, 01.10.2026 and EUR 9 [source: r-1]"


@pytest.mark.parametrize("value", VALUES)
def test_unwrap_gives_the_guard_what_wrap_rendered(value: Any) -> None:
    original = Untrusted(value, Provenance(tool=SEARCH, record_ids=("r-1", "r&2")))
    rebuilt = transcript.unwrap(wrap(original))
    assert rebuilt.provenance == original.provenance
    assert grounding.check(ANSWER, [rebuilt]) == grounding.check(ANSWER, [original])


def test_sources_skip_errors_and_keep_order() -> None:
    planted = sample("x", [hits((DEMO_NOTE, "a"))], errors=["get_portfolio"])
    found = transcript.sources(planted["messages"])
    assert [item.provenance.record_ids for item in found] == [(DEMO_NOTE,)]
    assert len(transcript.tool_texts(planted["messages"])) == 2


def test_tool_calls_and_reply_text() -> None:
    planted = sample("The answer.", [hits((DEMO_NOTE, "a"))], errors=["get_portfolio"])
    assert transcript.tool_calls(planted["replies"]) == [SEARCH, "get_portfolio"]
    assert transcript.reply_text(planted["replies"][-1]) == "The answer."


def test_a_check_does_not_change_the_recording() -> None:
    planted = good()
    before = copy.deepcopy(planted)
    transcript.check(planted, "a", 2)
    assert planted == before
