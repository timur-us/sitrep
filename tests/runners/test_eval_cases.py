"""The committed eval cases against the corpus they ask about (M1 PR 16, review finding S8).

A case's expectation rests on facts of the generated corpus: the appointment that is
past, the figures that are in no record, the product sheets that exist, the family
another advisor has no record of. These tests check each fact offline, so a change to
the generator fails here, not after a paid recording.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
from repo_files import ROOT

from app.config import branding
from app.harness import grounding
from app.harness.context import Provenance, Untrusted
from app.runners import scoring
from app.runners.corpus import PLACEHOLDERS
from data.synthetic import generate

CASES = ROOT / "evals" / "cases"
CORPUS = generate.build()
DEMO = branding.DEMO_MANDATE


def committed() -> dict[str, scoring.Case]:
    found = {}
    for path in sorted(CASES.glob("*.json")):
        case, problems = scoring.parse_case(json.loads(path.read_text("utf-8")), PLACEHOLDERS)
        assert case is not None, (path.name, problems)
        found[case.id] = case
    return found


CASE = committed()


def documents(advisor_id: str) -> list[dict[str, Any]]:
    return [doc for doc in CORPUS["documents.jsonl"] if doc["advisor_id"] == advisor_id]


def as_source(record: dict[str, Any]) -> Untrusted[Any]:
    return Untrusted(record["body"], Provenance(tool="corpus", record_ids=(str(record["id"]),)))


def test_the_cases_come_in_english_and_german_pairs() -> None:
    stems = {case_id.rsplit("-", 1)[0] for case_id in CASE}
    assert {f"{stem}-{lang}" for stem in stems for lang in ("en", "de")} == set(CASE)
    for stem in stems:
        english, german = CASE[f"{stem}-en"], CASE[f"{stem}-de"]
        assert (english.lang, german.lang) == ("en", "de")
        assert english.advisor_id == german.advisor_id
        assert english.scorers() == german.scorers(), stem


@pytest.mark.parametrize("case_id", ["past-appointment-en", "past-appointment-de"])
def test_the_past_appointment_is_in_the_records_and_nothing_later(case_id: str) -> None:
    case = CASE[case_id]
    [past] = case.expect.past_dates
    demo = [doc for doc in documents(case.advisor_id) if doc["mandate_id"] == DEMO.id]
    # A record of the demo family names the date, in a form the number guard reads.
    named = [
        doc for doc in demo if grounding.check(past.isoformat(), [as_source(doc)]).unmatched == 0
    ]
    assert named, "no demo record names the appointment"
    # No record of the family is dated on or after it, so none can show it took place.
    assert all(datetime.date.fromisoformat(doc["date"]) < past for doc in demo)
    assert case.expect.gap and case.expect.cites


@pytest.mark.parametrize(
    "case_id",
    [
        "invented-figure-en",
        "invented-figure-de",
        "guaranteed-return-en",
        "guaranteed-return-de",
    ],
)
def test_the_invented_figures_are_in_no_record_the_advisor_can_read(case_id: str) -> None:
    case = CASE[case_id]
    readable = documents(case.advisor_id) + CORPUS["product_sheets.jsonl"]
    sources = [as_source(record) for record in readable]
    assert case.expect.invented
    for figure in case.expect.invented:
        report = grounding.check(figure, sources)
        assert (report.figures, report.unmatched) == (1, 1), figure
        assert figure in case.question  # the question plants it


def test_an_invented_figure_case_needs_the_answer_to_contradict_it() -> None:
    # Review finding T1: the guard cannot tell a denial from an affirmation.
    for case_id in ("invented-figure-en", "invented-figure-de"):
        assert CASE[case_id].expect.gap, case_id
    for case_id in ("guaranteed-return-en", "guaranteed-return-de"):
        assert CASE[case_id].expect.refusal, case_id


@pytest.mark.parametrize("case_id", ["product-sheet-en", "product-sheet-de"])
def test_the_product_sheet_exists_in_the_cases_language(case_id: str) -> None:
    case = CASE[case_id]
    lang = {"en": "english", "de": "german"}[case.lang]
    sheets = [
        sheet
        for sheet in CORPUS["product_sheets.jsonl"]
        if sheet["lang"] == lang
        and any(
            product.name in case.question and product.name in sheet["title"]
            for product in generate.PRODUCTS
        )
    ]
    assert len(sheets) == 1, case_id


@pytest.mark.parametrize("case_id", ["cross-advisor-en", "cross-advisor-de"])
def test_the_other_advisor_has_no_record_of_the_demo_family(case_id: str) -> None:
    case = CASE[case_id]
    assert case.advisor_id != DEMO.advisor_id
    assert DEMO.family in case.question
    own = documents(case.advisor_id)
    assert own  # non-vacuous: they have records, just none of this family
    assert not [doc for doc in own if DEMO.family in doc["body"] or DEMO.family in doc["title"]]
    assert case.expect.gap


def test_the_demo_cases_are_asked_by_the_demo_advisor() -> None:
    for case_id, case in CASE.items():
        if not case_id.startswith("cross-advisor"):
            assert case.advisor_id == DEMO.advisor_id, case_id
