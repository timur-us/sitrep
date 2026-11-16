"""The committed v2 recordings, scored (M1 PR 16; Fable's decisions of 2026-11-16).

Timur recorded the 14 cases on 2026-11-16, three samples each (evals/results/v2.md).
These tests pin what the scorers make of them, so a scorer change that moves a sample
shows here by name: the five German answers to English questions fail `language`, every
German-case answer passes it, and the two system failures are the only other ones.
"""

from __future__ import annotations

import json

from repo_files import ROOT

from app.harness.prompt import PROMPT_VERSION
from app.runners import scoring, transcript
from app.runners.corpus import PLACEHOLDERS

RECORDED = ROOT / "evals" / "recorded" / PROMPT_VERSION
CASES = ROOT / "evals" / "cases"

# (case, sample) -> the scorers that fail it, after R1 to R5 and with `language`.
EXPECTED = {
    ("briefing-en", 1): ("language",),
    ("briefing-en", 2): ("language",),
    ("portfolio-en", 1): ("language",),
    ("product-sheet-en", 2): ("language",),
    ("past-appointment-en", 1): ("gap", "language"),
    ("invented-figure-en", 3): ("plain_text",),
}


def scored() -> dict[tuple[str, int], tuple[str, ...]]:
    found = {}
    for path in sorted(RECORDED.glob("*.json")):
        raw = json.loads((CASES / path.name).read_text(encoding="utf-8"))
        case, problems = scoring.parse_case(raw, PLACEHOLDERS)
        assert case is not None, problems
        recording = json.loads(path.read_text(encoding="utf-8"))
        assert transcript.check(recording, case.id, 3) == [], path.name
        assert scoring.bound(case, recording) == [], path.name
        for index, sample in enumerate(recording["samples"], start=1):
            assert scoring.integrity(sample) == [], (path.name, index)
            found[(case.id, index)] = scoring.score(case, sample).failed
    return found


def test_every_case_is_recorded_three_times() -> None:
    recorded = sorted(path.stem for path in RECORDED.glob("*.json"))
    assert recorded == sorted(path.stem for path in CASES.glob("*.json"))
    assert len(scored()) == 42


def test_the_failures_are_the_diagnosed_ones() -> None:
    failures = {key: failed for key, failed in scored().items() if failed}
    assert failures == EXPECTED


def test_language_on_the_recordings() -> None:
    # By each case's own language, not its file name: the German answers to English
    # questions fail, and every answer to a German case passes.
    langs = {case_id: lang for case_id, lang in case_languages()}
    flagged = {key for key, failed in scored().items() if "language" in failed}
    assert {key for key in flagged if langs[key[0]] == "de"} == set()
    assert {key for key in flagged if langs[key[0]] == "en"} == {
        ("briefing-en", 1),
        ("briefing-en", 2),
        ("portfolio-en", 1),
        ("product-sheet-en", 2),
        ("past-appointment-en", 1),
    }
    assert sum(lang == "de" for lang in langs.values()) == 7


def case_languages() -> list[tuple[str, str]]:
    found = []
    for path in sorted(CASES.glob("*.json")):
        case, _ = scoring.parse_case(json.loads(path.read_text(encoding="utf-8")), PLACEHOLDERS)
        assert case is not None
        found.append((case.id, case.lang))
    return found


def test_no_recorded_sample_fails_hard() -> None:
    assert not [key for key, failed in scored().items() if scoring.HARD & set(failed)]
