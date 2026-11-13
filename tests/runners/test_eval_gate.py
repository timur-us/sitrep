"""The eval gate over recorded runs, the estimate and the live run's refusals (M1 PR 16).

The gate scores the recording of each case at the current prompt version and passes when
no hard scorer failed and the share of passing samples reaches min_pass_rate. A missing,
stale or inconsistent recording fails it. `--estimate` and the refusals of `--live --yes`
run offline: no key is read from the repository, no socket opens.
"""

from __future__ import annotations

import json
import os
import socket
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from recorded_runs import (
    DEMO_NOTE,
    DEMO_REPORT,
    OTHER,
    OTHER_RECORD,
    PORTFOLIO,
    hits,
    portfolio,
    raw_case,
    recording,
    sample,
)

from app.config.settings import ENV_PREFIX, KEY_VARIABLE, env_name
from app.harness import loop
from app.harness.prompt import PROMPT_VERSION
from app.runners import estimate, evals, live

REPORT = "Gesamtvermögen: 5.922.761,95 EUR"
GOOD = f"Total EUR 5,922,761.95 [source: {DEMO_REPORT}]."
BAD = "Total EUR 6,100,000.00."


def suite(
    root: Path,
    cases: dict[str, dict[str, Any]],
    recordings: dict[str, Any],
    *,
    samples: int = 2,
    rate: float = 1.0,
    version: str = PROMPT_VERSION,
) -> Path:
    directory = root / "evals"
    (directory / "cases").mkdir(parents=True)
    (directory / "recorded" / version).mkdir(parents=True)
    gate = f"min_cases = {len(cases)}\nsamples = {samples}\nmin_pass_rate = {rate}\n"
    (directory / "gate.toml").write_text(gate)
    for case_id, body in cases.items():
        (directory / "cases" / f"{case_id}.json").write_text(json.dumps(body))
    for case_id, body in recordings.items():
        path = directory / "recorded" / version / f"{case_id}.json"
        path.write_text(json.dumps(body))
    return directory


def answered(case_id: str, *answers: str) -> dict[str, Any]:
    return recording(
        case_id, [sample(answer, [portfolio(DEMO_REPORT, REPORT)]) for answer in answers]
    )


def run(directory: Path, *flags: str) -> int:
    return evals.main(["--dir", str(directory), *flags])


def test_every_sample_passing_passes_with_a_scored_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = suite(
        tmp_path,
        {"a": raw_case("a", tools=[PORTFOLIO], cites=True)},
        {"a": answered("a", GOOD, GOOD)},
    )
    assert run(directory) == 0
    out, err = capsys.readouterr()
    assert err == "", err
    summary = (
        f"evals: prompt {PROMPT_VERSION}: 1 cases, 2 of 2 samples pass (100.0%),"
        " min_pass_rate 1.0; hard failures 0: the gate passes"
    )
    assert out.splitlines() == ["evals: a: 2/2 samples pass", summary]


def test_a_rate_below_the_bar_fails_and_names_the_scorer(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cases = {"a": raw_case("a"), "b": raw_case("b")}
    recordings = {"a": answered("a", GOOD, BAD), "b": answered("b", GOOD, GOOD)}
    assert run(suite(tmp_path / "strict", cases, recordings, rate=0.8)) == 1
    out = capsys.readouterr().out
    assert "evals: a: 1/2 samples pass; failed: sample 2: grounding" in out, out
    assert "3 of 4 samples pass (75.0%)" in out and "the gate fails" in out, out
    # The same recordings meet a lower bar.
    assert run(suite(tmp_path / "lenient", cases, recordings, rate=0.75)) == 0


def test_a_hard_failure_fails_the_gate_at_any_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cases = {"a": raw_case("a", advisor_id=OTHER), "b": raw_case("b")}
    leaked = recording("a", [sample("x", [hits((DEMO_NOTE, "note"))]), sample("x")])
    recordings = {"a": leaked, "b": answered("b", GOOD, GOOD)}
    assert run(suite(tmp_path, cases, recordings, rate=0.5)) == 1
    out = capsys.readouterr().out
    assert "failed: sample 1: no_foreign_record" in out, out
    assert "hard failures 1: the gate fails" in out, out


def test_a_missing_recording_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = suite(
        tmp_path, {"a": raw_case("a"), "b": raw_case("b")}, {"a": answered("a", GOOD, GOOD)}
    )
    assert run(directory) == 1
    out, err = capsys.readouterr()
    assert (
        err
        == f"evals: recorded/{PROMPT_VERSION}/b.json: missing (record it: --live --yes, by hand)\n"
    )
    assert out == ""


def test_a_recording_of_another_prompt_version_does_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Kept as evidence, checked for structure, never scored (ADR 0014).
    directory = suite(
        tmp_path, {"a": raw_case("a")}, {"a": answered("a", GOOD, GOOD)}, version="v0"
    )
    assert run(directory) == 1
    assert f"recorded/{PROMPT_VERSION}/a.json: missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda r: r.update(prompt_sha256="0" * 64), "$.prompt_sha256: stale"),
        (lambda r: r.update(tools_sha256="0" * 64), "$.tools_sha256: stale"),
        (lambda r: r.update(corpus_sha256="0" * 64), "$.corpus_sha256: stale"),
        (lambda r: r.update(model="another-model"), "$.model: stale"),
        (lambda r: r["samples"].pop(), "$.samples: not a list of 2"),
        (
            lambda r: r["samples"][1]["response"]["body"]["grounding"].update(unmatched=0),
            "$.samples[1].response.body.grounding.unmatched: not what the transcript gives",
        ),
    ],
)
def test_a_stale_short_or_inconsistent_recording_fails(
    change: Any, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    planted = answered("a", GOOD, BAD)
    change(planted)
    assert run(suite(tmp_path, {"a": raw_case("a")}, {"a": planted})) == 1
    err = capsys.readouterr().err
    assert f"evals: recorded/{PROMPT_VERSION}/a.json: {needle}" in err, err


def test_errors_never_carry_recorded_text(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    canary = "cnry-recorded-text-7f3a"
    planted = recording("a", [sample(f"{canary} {BAD}", [hits((OTHER_RECORD, canary))])])
    planted["samples"][0]["response"]["body"]["grounding"]["figures"] = 99
    assert run(suite(tmp_path, {"a": raw_case("a")}, {"a": planted}, samples=1)) == 1
    out, err = capsys.readouterr()
    assert canary not in out + err and OTHER_RECORD not in out + err


@pytest.mark.parametrize(
    ("gate", "needle"),
    [
        ("min_cases = 0\n", "samples must be an integer from 1 to 10"),
        ("min_cases = 0\nsamples = 0\nmin_pass_rate = 1.0\n", "samples must be"),
        ("min_cases = 0\nsamples = 11\nmin_pass_rate = 1.0\n", "samples must be"),
        ("min_cases = 0\nsamples = true\nmin_pass_rate = 1.0\n", "samples must be"),
        ("min_cases = 0\nsamples = 3\n", "min_pass_rate must be a float"),
        ("min_cases = 0\nsamples = 3\nmin_pass_rate = 1\n", "min_pass_rate must be a float"),
        ("min_cases = 0\nsamples = 3\nmin_pass_rate = 1.5\n", "min_pass_rate must be a float"),
        ("min_cases = 0\nsamples = 3\nmin_pass_rate = 0.8\nhard = []\n", "unknown keys"),
    ],
)
def test_a_broken_eval_gate_fails(
    gate: str, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = suite(tmp_path, {}, {})
    (directory / "gate.toml").write_text(gate)
    assert run(directory) == 1
    assert needle in capsys.readouterr().err


def test_the_committed_gate_is_pre_registered() -> None:
    import tomllib

    from repo_files import ROOT

    gate = tomllib.loads((ROOT / "evals" / "gate.toml").read_text(encoding="utf-8"))
    assert gate == {"min_cases": 14, "samples": 3, "min_pass_rate": 0.8}, gate


# --- Flags ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "needle"),
    [
        (("--live",), "--live needs --yes"),
        (("--yes",), "--yes only goes with --live"),
        (("--live", "--yes", "--estimate"), "--estimate comes before --live"),
        (("--case", "a"), "--case goes with --live or --estimate"),
        (("--estimate", "--case", "nope"), "--case names no valid case: nope"),
    ],
)
def test_flag_combinations_are_refused(
    flags: tuple[str, ...], needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = suite(tmp_path, {"a": raw_case("a")}, {})
    code = run(directory, *flags)
    assert code in (1, 2) and needle in capsys.readouterr().err


def test_a_live_run_with_no_case_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(suite(tmp_path, {}, {}), "--live", "--yes") == 2
    assert "no case to record" in capsys.readouterr().err


# --- The estimate ---------------------------------------------------------------------


def block_sockets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def call(*args: object, **kwargs: object) -> None:
            attempts.append(name)
            raise OSError(f"test: {name} refused")

        return call

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse(name))
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse(name))
    return attempts


def test_the_estimate_prices_the_assumed_tokens_without_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    attempts = block_sockets(monkeypatch)
    directory = suite(tmp_path, {"a": raw_case("a"), "b": raw_case("b")}, {}, samples=3)
    assert run(directory, "--estimate") == 0
    out = capsys.readouterr().out.splitlines()
    assert attempts == []
    # 6 asks x (12,000 x USD 2 + 1,500 x USD 10) per million x 1.1 = USD 0.2574.
    assert out == [
        f"evals: estimate, prompt {PROMPT_VERSION}, claude-sonnet-5-5: 2 cases x 3 samples = 6 asks",
        f"evals: tokens per ask: 12000 input, 1500 output (assumed: no {PROMPT_VERSION} recording yet)",
        "evals: expected cost: USD 0.26 (the price table of 2026-11-09, at the US-only rate)",
        f"evals: ceiling: USD 4.58 (every ask spends its whole budget of {loop.REQUEST_BUDGET} tokens)",
    ]


def test_the_estimate_uses_measured_tokens_where_a_recording_has_them(tmp_path: Path) -> None:
    directory = suite(
        tmp_path, {"a": raw_case("a"), "b": raw_case("b")}, {"a": answered("a", GOOD, GOOD)}
    )
    result = estimate.estimate(directory, ["a", "b"], 2)
    # a: the planted usage, 5,000 in and 400 out; b: assumed.
    per_ask = {
        "a": (5_000 * 2 + 400 * 10) * Decimal("1.1") / 1_000_000,
        "b": (12_000 * 2 + 1_500 * 10) * Decimal("1.1") / 1_000_000,
    }
    assert result.measured_cases == 1
    assert result.tokens == {"input_tokens": 8_500, "output_tokens": 950}
    assert result.expected_usd == (2 * sum(per_ask.values())).quantize(
        Decimal("0.01"), "ROUND_CEILING"
    )
    assert estimate.lines(result, 2, 2)[1].endswith(
        "(measured for 1 of 2 cases, assumed for the rest)"
    )


def test_the_ceiling_is_the_whole_budget_with_the_most_output_the_turns_allow() -> None:
    output = loop.MAX_TURNS * loop.MAX_TOKENS
    expected = ((loop.REQUEST_BUDGET - output) * 2 + output * 10) * Decimal("1.1") / 1_000_000
    assert estimate.ceiling_per_ask() == expected


def test_the_estimate_can_cover_some_cases(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = suite(tmp_path, {"a": raw_case("a"), "b": raw_case("b")}, {}, samples=1)
    assert run(directory, "--estimate", "--case", "b") == 0
    assert "1 cases x 1 samples = 1 asks" in capsys.readouterr().out


# --- The live run's refusals, before any call -------------------------------------------


@pytest.fixture
def no_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory without .env, an environment without the key."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    for name in list(os.environ):
        if name.upper().startswith(("ANTHROPIC_", ENV_PREFIX)):
            monkeypatch.delenv(name)
    return workdir


def live_suite(tmp_path: Path) -> Path:
    return suite(tmp_path / "suite", {"a": raw_case("a")}, {})


def test_live_without_a_key_exits_2_before_any_socket(
    no_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    attempts = block_sockets(monkeypatch)
    directory = live_suite(tmp_path)
    assert run(directory, "--live", "--yes") == 2
    assert attempts == []
    assert f"evals: live: refused: {KEY_VARIABLE} is not set" in capsys.readouterr().err
    assert not (directory / "recorded" / PROMPT_VERSION / "a.json").exists()


def test_live_refuses_an_exported_key(
    no_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(KEY_VARIABLE, "dummy-not-a-key")
    assert run(live_suite(tmp_path), "--live", "--yes") == 2
    err = capsys.readouterr().err
    assert "reads the key from .env only" in err and "dummy" not in err


def test_live_refuses_an_sdk_variable(
    no_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    assert run(live_suite(tmp_path), "--live", "--yes") == 2
    assert "ANTHROPIC_BASE_URL" in capsys.readouterr().err


def test_live_reads_the_key_from_env_file_and_refuses_another_model(
    no_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The key comes from .env in the working directory, as python -m app reads it; a
    # recording of a model other than the default would be stale, so nothing is called.
    (no_env / ".env").write_text(f"{KEY_VARIABLE}=dummy-not-a-key\n")
    monkeypatch.setenv(env_name("model"), "claude-sonnet-5-5")

    class Other:
        model = "another-model"

    seen: list[object] = []

    def build(settings: Any) -> Other:
        seen.append(settings.anthropic_api_key.get_secret_value())
        return Other()

    monkeypatch.setattr(live, "build_model_client", build)
    assert run(live_suite(tmp_path), "--live", "--yes") == 2
    assert seen == ["dummy-not-a-key"]
    assert "another model's would be stale" in capsys.readouterr().err


def test_live_refuses_plaintext_traces(
    no_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(env_name("trace_plaintext"), "1")

    class Default:
        model = "claude-sonnet-5-5"

    monkeypatch.setattr(live, "build_model_client", lambda settings: Default())
    assert run(live_suite(tmp_path), "--live", "--yes") == 2
    assert "plaintext traces" in capsys.readouterr().err


def test_the_recording_client_copies_each_request_and_reply() -> None:
    class Inner:
        model = "m"

        def create(self, **request: Any) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "a"}], "usage": {"input_tokens": 1}}

    recorder = live.RecordingClient(Inner())
    messages = [{"role": "user", "content": "q"}]
    reply = recorder.create(system="s", messages=messages, tools=[], max_tokens=1)
    messages.append({"role": "assistant", "content": "changed later"})
    reply["content"].clear()
    assert recorder.requests == [
        {
            "system": "s",
            "messages": [{"role": "user", "content": "q"}],
            "tools": [],
            "max_tokens": 1,
        }
    ]
    assert recorder.replies[0]["content"] == [{"type": "text", "text": "a"}]
    assert recorder.model == "m"


def test_the_runner_records_into_the_current_versions_directory(tmp_path: Path) -> None:
    planted = answered("a", GOOD)
    path = live.write(tmp_path, planted)
    assert path == tmp_path / "recorded" / PROMPT_VERSION / "a.json"
    assert json.loads(path.read_text(encoding="utf-8")) == planted
