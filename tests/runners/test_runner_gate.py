"""The eval and red-team runners, in-process through main(argv).

In M0 a runner checks the corpus and the gate, offline, and scores nothing: an empty
corpus passes with min_cases = 0, and anything it cannot check fails. Live runs are
refused. Error output names the file and the rule, never the file's text.
"""

from __future__ import annotations

import json
import socket
import tomllib
from pathlib import Path
from typing import Any

import pytest
from repo_files import ROOT

from app.config import branding
from app.runners import evals, redteam

RUNNERS = {"evals": (evals, "cases"), "redteam": (redteam, "attacks")}


def make_suite(
    root: Path,
    suite: str,
    *,
    min_cases: Any = 0,
    cases: dict[str, Any] | None = None,
    recordings: dict[str, Any] | None = None,
) -> Path:
    """A suite directory: gate.toml, the case directory and recorded/."""
    _, case_dir = RUNNERS[suite]
    directory = root / suite
    (directory / case_dir).mkdir(parents=True)
    (directory / "recorded").mkdir()
    (directory / "gate.toml").write_text(f"min_cases = {json.dumps(min_cases)}\n")
    for rel, body in (cases or {}).items():
        _write(directory / case_dir / rel, body)
    for rel, body in (recordings or {}).items():
        _write(directory / "recorded" / rel, body)
    return directory


def _write(path: Path, body: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def run(suite: str, directory: Path, *extra: str) -> int:
    module, _ = RUNNERS[suite]
    return module.main(["--dir", str(directory), *extra])


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_empty_corpus_passes(suite: str, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(suite, make_suite(tmp_path, suite)) == 0
    out, err = capsys.readouterr()
    assert out == f"{suite}: no cases (min_cases=0)\n", out
    assert err == "", err


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_the_committed_suite_passes(suite: str, capsys: pytest.CaptureFixture) -> None:
    # `make evals` and `make redteam` run without --dir, on the committed corpus.
    module, _ = RUNNERS[suite]
    assert module.main([]) == 0
    assert capsys.readouterr().out == f"{suite}: no cases (min_cases=0)\n"


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_gate_ratchet(suite: str) -> None:
    # min_cases equals the committed case count, so a deleted case fails the gate.
    _, case_dir = RUNNERS[suite]
    gate = ROOT / suite / "gate.toml"
    assert gate.is_file(), f"{suite}/gate.toml is missing"
    min_cases = tomllib.loads(gate.read_text(encoding="utf-8")).get("min_cases")
    committed = sorted((ROOT / suite / case_dir).glob("*.json"))
    assert min_cases == len(committed), f"{suite}: min_cases {min_cases}, {len(committed)} cases"


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_min_cases_above_the_count_fails(
    suite: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run(suite, make_suite(tmp_path, suite, min_cases=1)) == 1
    assert "0 cases, min_cases=1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("gate", "needle"),
    [
        (None, "gate.toml is missing"),
        ("min_cases = -1\n", "min_cases must be an integer >= 0"),
        ("min_cases = true\n", "min_cases must be an integer >= 0"),
        ('min_cases = "0"\n', "min_cases must be an integer >= 0"),
        ("min_cases = 0\nmax_cases = 3\n", "unknown keys"),
        ("min_cases = \n", "gate.toml is not valid TOML"),
    ],
)
def test_broken_gate_fails(
    gate: str | None, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = make_suite(tmp_path, "evals")
    if gate is None:
        (directory / "gate.toml").unlink()
    else:
        (directory / "gate.toml").write_text(gate)
    assert run("evals", directory) == 1
    assert needle in capsys.readouterr().err


@pytest.mark.parametrize(
    ("suite", "where", "rel", "reported"),
    [
        ("evals", "cases", "notes.txt", "cases/notes.txt"),
        ("evals", "cases", "nested/case.json", "cases/nested"),
        ("redteam", "attacks", "attack.yaml", "attacks/attack.yaml"),
        ("redteam", "recorded", "loose.json", "recorded/loose.json"),
        ("evals", "recorded", "v1/notes.md", "recorded/v1/notes.md"),
        ("redteam", "recorded", "v1/deeper/a.json", "recorded/v1/deeper"),
        ("evals", "recorded", "Bad Version/a.json", "recorded/Bad Version"),
    ],
)
def test_stray_file_fails(
    suite: str,
    where: str,
    rel: str,
    reported: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    directory = make_suite(tmp_path, suite)
    _write(directory / where / rel, "{}")
    assert run(suite, directory) == 1
    err = capsys.readouterr().err
    assert f"{suite}: {reported}: stray" in err, err


def test_a_file_name_cannot_start_a_workflow_command(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # GitHub reads "::error ...::" at the start of a log line as a command. A name with
    # a newline must not put one there.
    directory = make_suite(tmp_path, "evals")
    _write(directory / "cases" / "a\n::error file=x::forged.json", "{}")
    assert run("evals", directory) == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines and all(line.startswith("evals: ") for line in lines), err
    assert "\\n::error" in err, err  # shown escaped


def test_missing_case_directory_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = make_suite(tmp_path, "redteam")
    (directory / "attacks").rmdir()
    assert run("redteam", directory) == 1
    assert "attacks/ is missing" in capsys.readouterr().err


def test_symlinked_case_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = make_suite(tmp_path, "evals")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "a"}))
    (directory / "cases" / "a.json").symlink_to(outside)
    assert run("evals", directory) == 1
    assert "symlink" in capsys.readouterr().err


def test_finder_metadata_and_gitkeep_are_ignored(tmp_path: Path) -> None:
    directory = make_suite(tmp_path, "evals", cases={".gitkeep": "", ".DS_Store": "x"})
    _write(directory / "recorded" / ".gitkeep", "")
    assert run("evals", directory) == 0


def test_duplicate_id_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cases = {"a.json": {"id": "same"}, "b.json": {"id": "same"}}
    assert run("evals", make_suite(tmp_path, "evals", min_cases=2, cases=cases)) == 1
    assert "duplicate id" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("{not json", "not valid JSON"),
        ('{"id": "a", "id": "b"}', "duplicate key"),
        ("[1, 2]", "not a JSON object"),
        ({"text": "no id"}, "id must be"),
        ({"id": "Has Spaces"}, "id must be"),
        ({"id": 7}, "id must be"),
    ],
)
def test_invalid_case_fails(
    body: Any, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = make_suite(tmp_path, "evals", min_cases=1, cases={"a.json": body})
    assert run("evals", directory) == 1
    assert needle in capsys.readouterr().err


def test_case_that_is_not_utf8_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = make_suite(tmp_path, "evals", min_cases=1)
    (directory / "cases" / "a.json").write_bytes(b'{"id": "a", "t": "\xff"}')
    assert run("evals", directory) == 1
    assert "not UTF-8" in capsys.readouterr().err


def test_orphan_recording_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    directory = make_suite(
        tmp_path,
        "redteam",
        min_cases=1,
        cases={"a.json": {"id": "a"}},
        recordings={"v1/a.json": {}, "v1/gone.json": {}},
    )
    assert run("redteam", directory) == 1
    err = capsys.readouterr().err
    assert "recorded/v1/gone.json: no case with this id" in err, err
    assert "recorded/v1/a.json" not in err, err


@pytest.mark.parametrize(
    "recording",
    [
        {"request": {"headers": {"authorization": "Bearer abc"}}},
        {"request": {"headers": {"Authorization": "Bearer abc"}}},
        {"request": {"headers": {"x-api-key": "abc"}}},
        {"turns": [{"request": {"headers": {"X-Api-Key": "abc"}}}]},
        {"response": {"headers": {"set-cookie": "s=abc"}}},
        {"request": {"headers": [{"name": "Authorization", "value": "Bearer abc"}]}},
        {"request": {"headers": [["x-api-key", "abc"]]}},
        {"request": {"headers": {"api-key": "abc"}}},
        {"request": {"headers": {"Ocp-Apim-Subscription-Key": "abc"}}},
    ],
)
def test_recording_with_a_credential_header_fails(
    recording: dict, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    directory = make_suite(
        tmp_path,
        "evals",
        min_cases=1,
        cases={"a.json": {"id": "a"}},
        recordings={"v1/a.json": recording},
    )
    assert run("evals", directory) == 1
    err = capsys.readouterr().err
    assert "recorded/v1/a.json" in err and "credential header" in err, err
    assert "abc" not in err, "the error shows the header's value"


def test_a_valid_corpus_fails_closed_in_m0(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # Nothing scores a case before M1, so a corpus with cases cannot pass the gate.
    directory = make_suite(
        tmp_path,
        "evals",
        min_cases=1,
        cases={"a.json": {"id": "a", "question": "x"}},
        recordings={"v1/a.json": {"answer": "y"}},
    )
    assert run("evals", directory) == 1
    out, err = capsys.readouterr()
    assert out == "evals: cases=1 (min_cases=1); scoring arrives in M1, so the gate fails closed\n"
    assert err == "", err


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_live_without_yes_exits_2(
    suite: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run(suite, make_suite(tmp_path, suite), "--live") == 2
    assert "--live needs --yes" in capsys.readouterr().err


@pytest.mark.parametrize("suite", sorted(RUNNERS))
def test_live_with_yes_is_not_available(
    suite: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert run(suite, make_suite(tmp_path, suite), "--live", "--yes") == 2
    assert "not available in M0" in capsys.readouterr().err


def test_yes_without_live_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run("evals", make_suite(tmp_path, "evals"), "--yes") == 2
    assert "--yes only goes with --live" in capsys.readouterr().err


def test_a_dummy_key_causes_no_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def refuse(name: str):
        def call(*args: object, **kwargs: object) -> None:
            attempts.append(name)
            raise OSError(f"test: {name} refused")

        return call

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse(name))
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse(name))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-key")

    empty = make_suite(tmp_path / "a", "evals")
    full = make_suite(tmp_path / "b", "redteam", min_cases=1, cases={"a.json": {"id": "a"}})
    assert run("evals", empty) == 0
    assert run("redteam", full) == 1
    assert run("evals", empty, "--live", "--yes") == 2
    assert attempts == [], attempts


def fullwidth(text: str) -> str:
    """ASCII letters as their fullwidth forms, which NFKC maps back."""
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


# Placeholders and literals. Strings are built from branding, so this file names no
# cast member or domain itself.
ADVISOR = next(person for person in branding.CAST if person.id == branding.DEMO_MANDATE.advisor_id)
ATTACKER = next(person for person in branding.CAST if person.role is branding.Role.ATTACKER)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (f"Hello {ADVISOR.name}", "literal cast name"),
        (f"hello {ATTACKER.name.upper()}!", "literal cast name"),
        (f"from {branding.DEMO_MANDATE.family}", "literal demo mandate"),
        (f"see {branding.DOMAINS.canary.upper()}.", "literal domain"),
        (f"write to {ADVISOR.email}", "literal domain"),
        (f"{branding.BANK.display_name} says", "literal bank name"),
        (f"{branding.PRODUCT.name} says", "literal product name"),
        (f"Hello {fullwidth(ADVISOR.name)}", "literal cast name"),
        (f"Hello {ADVISOR.name[0]}\u200b{ADVISOR.name[1:]}", "literal cast name"),
        ("{advisor.nobody} or {domain.elsewhere}", "unknown placeholder"),
        (f"{{advisor.{ATTACKER.id}}}", "unknown placeholder"),
    ],
)
def test_literal_or_unknown_names_fail(
    text: str, needle: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    for suite, (_, case_dir) in RUNNERS.items():
        directory = make_suite(
            tmp_path / suite, suite, min_cases=1, cases={"a.json": {"id": "a", "text": text}}
        )
        assert run(suite, directory) == 1
        err = capsys.readouterr().err
        assert f"{case_dir}/a.json: $.text: {needle}" in err, err
        assert text not in err, "the error shows the case's text"


def test_literals_are_found_in_keys_and_escapes(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    escaped = "\\u00" + format(ord(ADVISOR.name[0]), "x") + ADVISOR.name[1:]
    body = '{"id": "a", "' + escaped + '": 1, "list": ["ok", "' + escaped + '"]}'
    directory = make_suite(tmp_path, "evals", min_cases=1, cases={"a.json": body})
    assert run("evals", directory) == 1
    err = capsys.readouterr().err
    assert "cases/a.json: $.<key>: literal cast name" in err, err
    assert "cases/a.json: $.list[1]: literal cast name" in err, err


def test_placeholders_pass(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    text = (
        f"{{advisor.{ADVISOR.id}}} asks {{cast.{ATTACKER.id}}} about {{mandate.demo}}; "
        f"mail {{email.{ADVISOR.id}}}, see https://{{domain.canary}}/x?q=1 and {{product.name}}"
    )
    directory = make_suite(
        tmp_path, "redteam", min_cases=1, cases={"a.json": {"id": "a", "text": text}}
    )
    assert run("redteam", directory) == 1  # valid, so it fails closed only for scoring
    out, err = capsys.readouterr()
    assert err == "", err
    assert "scoring arrives in M1" in out
