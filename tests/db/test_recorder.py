"""The live recorder through the real app, with a scripted model (M1 PR 16, ADR 0014).

The positive path of the plan: a committed case is recorded the way `--live --yes`
records it, through create_app() with the dev login, POST /dev/login and POST /api/ask
over ASGI from 127.0.0.1, the real READ handlers on the loaded corpus and row-level
security, and the offline gate then scores the recording and passes with a scored line.
Only the model is scripted (tests/scripted_model.py): no key, no socket but the database's.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from loaded_corpus import ensure_loaded
from repo_files import ROOT
from scripted_model import SEARCH, USAGE, EchoModel, reply, tool_use

from app.api import ask
from app.config import branding
from app.config.settings import LOOPBACK, Settings
from app.db import load
from app.db.session import GuardError
from app.embeddings.onnx import OnnxEmbedder
from app.harness.context import Provenance, Untrusted
from app.harness.model_client import ModelCallError, cost_usd
from app.harness.prompt import PROMPT_VERSION, system_prompt
from app.main import create_app
from app.runners import evals, live, scoring, transcript
from app.runners.corpus import PLACEHOLDERS

pytestmark = pytest.mark.usefixtures("database")

TODAY = datetime.date(2026, 11, 13)
DEMO = branding.DEMO_MANDATE
CASES = ROOT / "evals" / "cases"


@pytest.fixture(scope="module")
def corpus(
    database: None, embedder: OnnxEmbedder, tmp_path_factory: pytest.TempPathFactory
) -> load.Corpus:
    return ensure_loaded(embedder, tmp_path_factory.mktemp("corpus"))


@pytest.fixture
def settings() -> Settings:
    # As live._settings() builds them, without reading a .env file.
    return Settings(_env_file=None, dev_auth="1", host=LOOPBACK, port=live.PORT)


def committed(case_id: str) -> scoring.Case:
    raw = json.loads((CASES / f"{case_id}.json").read_text(encoding="utf-8"))
    case, problems = scoring.parse_case(raw, PLACEHOLDERS)
    assert case is not None, problems
    return case


def record(
    case: scoring.Case, embedder: OnnxEmbedder, settings: Settings, samples: int = 1
) -> tuple[dict[str, Any], EchoModel, list[str]]:
    model = EchoModel(DEMO.family)
    lines: list[str] = []
    recording = live.record_case(
        case,
        samples=samples,
        client=model,
        embedder=embedder,
        settings=settings,
        today=TODAY,
        report=lines.append,
    )
    return recording, model, lines


def test_a_recorded_case_passes_the_gate_with_a_scored_line(
    corpus: load.Corpus,
    embedder: OnnxEmbedder,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    case = committed("briefing-en")
    recording, model, lines = record(case, embedder, settings, samples=2)
    assert len(model.calls) == 4  # two asks, a search and an answer each
    per_ask = 2 * cost_usd(model.model, USAGE)  # the scripted search and answer
    assert lines == [
        f"evals: live: briefing-en sample {n}/2: 200 end_turn USD {per_ask}" for n in (1, 2)
    ]
    # The date reached the prompt through the route: the stale check renders it again.
    for sample in recording["samples"]:
        assert sample["date"] == "2026-11-13"
        assert sample["system"] == system_prompt(TODAY)
    assert transcript.check(recording, "briefing-en", 2) == []
    # The suite: the committed case, the recording where make evals reads it.
    directory = tmp_path / "evals"
    (directory / "cases").mkdir(parents=True)
    shutil.copy(CASES / "briefing-en.json", directory / "cases")
    (directory / "gate.toml").write_text("min_cases = 1\nsamples = 2\nmin_pass_rate = 1.0\n")
    path = live.write(directory, recording)
    assert path == directory / "recorded" / PROMPT_VERSION / "briefing-en.json"
    assert evals.main(["--dir", str(directory)]) == 0
    out, err = capsys.readouterr()
    assert err == "", err
    assert "evals: briefing-en: 2/2 samples pass" in out.splitlines(), out
    assert out.splitlines()[-1].endswith("hard failures 0: the gate passes"), out


def test_the_recording_holds_what_the_model_was_sent_and_nothing_more(
    corpus: load.Corpus, embedder: OnnxEmbedder, settings: Settings
) -> None:
    recording, model, _ = record(committed("briefing-de"), embedder, settings)
    [sample] = recording["samples"]
    last = model.calls[-1]
    assert sample["messages"] == last["messages"] and sample["system"] == last["system"]
    assert recording["tools_sha256"] == transcript.tools_sha256(last["tools"])
    assert recording["tools_sha256"] == transcript.current()["tools_sha256"]
    assert len(sample["replies"]) == len(model.calls)
    # The question went in as the case resolved it; the answer came back as the route's.
    assert sample["messages"][0] == {"role": "user", "content": committed("briefing-de").question}
    assert sample["response"]["status"] == 200
    assert set(sample["response"]["body"]) >= {"answer", "citations", "grounding", "trace_id"}
    # No credential or session reaches the recording.
    text = json.dumps(recording)
    for needle in ("cookie", "authorization", "x-api-key", "_dev_session"):
        assert needle not in text.lower(), needle


def test_another_advisor_recorded_on_the_demo_family_reads_none_of_its_records(
    corpus: load.Corpus, embedder: OnnxEmbedder, settings: Settings
) -> None:
    case = committed("cross-advisor-en")
    assert case.advisor_id != DEMO.advisor_id
    recording, _, _ = record(case, embedder, settings)
    [sample] = recording["samples"]
    sources = transcript.sources(sample["messages"])
    seen = {record for item in sources for record in item.provenance.record_ids}
    owners = scoring.owners()
    # Non-vacuous: the search found records, all the asking advisor's or product sheets.
    assert seen and {owners[record] for record in seen} <= {case.advisor_id, None}
    result = scoring.score(case, sample)
    assert "no_foreign_record" not in result.failed, result
    # The scripted answer says no gap, so that scorer fails, and only that one.
    assert result.failed == ("gap",), result


# --- Review round (M1 PR 16) -----------------------------------------------------------


def test_a_broken_boundary_is_recorded_as_guard_and_fails_hard(
    corpus: load.Corpus,
    embedder: OnnxEmbedder,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F2: a GuardError in a handler is a 500 with a fixed body; the recorder keeps the
    # loop's error word from the request's span, and the scorer fails it hard.
    def guarded(pool: object, embedder: object) -> dict[str, Any]:
        def handler(advisor: object, arguments: object) -> Any:
            raise GuardError("a row of another advisor reached a READ handler")

        return {SEARCH: handler, "get_portfolio": handler}

    monkeypatch.setattr(ask, "read_handlers", guarded)
    case = committed("briefing-en")
    recording, _, _ = record(case, embedder, settings)
    [sample] = recording["samples"]
    assert (sample["response"]["status"], sample["error"]) == (500, "guard")
    result = scoring.score(case, sample)
    assert "no_foreign_record" in result.failed and result.hard


def test_a_failed_call_keeps_what_was_sent_and_the_run_goes_on(
    corpus: load.Corpus, embedder: OnnxEmbedder, settings: Settings
) -> None:
    # F3: the second call fails after the search; the request it carried, with the tool
    # result, is in the transcript, and no_foreign_record reads it.
    model = EchoModel(
        DEMO.family,
        script=[
            reply(tool_use("tu_1", SEARCH, {"query": DEMO.family}), stop="tool_use"),
            ModelCallError("response", cause="unreadable"),
        ],
    )
    case = committed("cross-advisor-en")
    recording = live.record_case(
        case,
        samples=1,
        client=model,
        embedder=embedder,
        settings=settings,
        today=TODAY,
        report=lambda line: None,
    )
    [sample] = recording["samples"]
    assert sample["response"]["status"] == 502 and len(sample["replies"]) == 1
    assert len(transcript.tool_texts(sample["messages"])) == 1
    assert sample["messages"] == model.calls[-1]["messages"]
    assert "no_foreign_record" not in scoring.score(case, sample).failed


def test_a_foreign_record_in_a_failed_calls_request_fails_hard(
    corpus: load.Corpus,
    embedder: OnnxEmbedder,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F3, the positive side: a search that (by a planted break) returns a record of the
    # demo advisor to another advisor, then a call that fails. The leak reached the
    # provider in the failed request; the transcript keeps it and the scorer finds it.
    leaked = f"{DEMO.id}-report"

    def leaking(pool: object, embedder: object) -> dict[str, Any]:
        def search(advisor: object, arguments: object) -> Untrusted[Any]:
            return Untrusted(
                [{"document_id": leaked}], Provenance(tool=SEARCH, record_ids=(leaked,))
            )

        return {SEARCH: search, "get_portfolio": search}

    monkeypatch.setattr(ask, "read_handlers", leaking)
    model = EchoModel(
        DEMO.family,
        script=[
            reply(tool_use("tu_1", SEARCH, {"query": DEMO.family}), stop="tool_use"),
            ModelCallError("response", cause="unreadable"),
        ],
    )
    case = committed("cross-advisor-en")
    recording = live.record_case(
        case,
        samples=1,
        client=model,
        embedder=embedder,
        settings=settings,
        today=TODAY,
        report=lambda line: None,
    )
    [sample] = recording["samples"]
    assert sample["response"]["status"] == 502
    result = scoring.score(case, sample)
    assert "no_foreign_record" in result.failed and result.hard


def test_the_route_reads_the_date_for_every_ask(
    corpus: load.Corpus, embedder: OnnxEmbedder, settings: Settings
) -> None:
    # T5: one app, two asks, the clock moves between them: each prompt has its day.
    days = iter([datetime.date(2026, 11, 13), datetime.date(2026, 11, 14)])
    model = EchoModel(DEMO.family)
    app = create_app(
        settings,
        bind_host=LOOPBACK,
        model_client=model,
        embedder=embedder,
        today=lambda: next(days),
    )
    question = committed("briefing-en").question

    async def twice() -> list[int]:
        first, _ = await live._ask(app, DEMO.advisor_id, question)
        second, _ = await live._ask(app, DEMO.advisor_id, question)
        return [first, second]

    assert asyncio.run(live._served(app, twice)) == [200, 200]
    dates = [call["system"] for call in model.calls]
    assert dates[:2] == [system_prompt(datetime.date(2026, 11, 13))] * 2
    assert dates[2:] == [system_prompt(datetime.date(2026, 11, 14))] * 2
