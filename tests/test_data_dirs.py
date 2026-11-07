"""The data directories are inert: data extensions only, no code, no executable bit."""

from __future__ import annotations

import os
from pathlib import Path

import data_dirs
import pytest
from data_dirs import DATA_DIRS, DATA_SUFFIXES, violations
from repo_files import ROOT, git

from data.synthetic import generate

# Committed in PR 4 with a .gitkeep each. data/synthetic/templates holds templates
# (PR 8), and data/synthetic/out is generated and git-ignored: both are checked below.
COMMITTED = ("evals/cases", "evals/recorded", "redteam/attacks", "redteam/recorded")
CORPUS_FILES = [
    "advisors.jsonl",
    "documents.jsonl",
    "mandates.jsonl",
    "manifest.json",
    "product_sheets.jsonl",
]


def test_data_dirs_are_declared() -> None:
    expected = {"evals/cases", "evals/recorded", "redteam/attacks", "redteam/recorded"}
    expected |= {"data/synthetic/templates", "data/synthetic/out"}
    assert set(DATA_DIRS) == expected, f"declared data dirs: {DATA_DIRS}"
    assert DATA_SUFFIXES == {".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt"}, (
        f"declared suffixes: {sorted(DATA_SUFFIXES)}"
    )
    # Run reports (the eval-run and redteam-run skills) are Markdown and nothing else.
    reports = getattr(data_dirs, "REPORT_DIRS", ())
    assert set(reports) == {"evals/results", "redteam/results"}, f"report dirs: {reports}"
    assert getattr(data_dirs, "REPORT_SUFFIXES", None) == {".md"}


def test_data_dirs_are_inert() -> None:
    assert not violations(ROOT), "\n".join(violations(ROOT))


def test_committed_data_dirs_exist() -> None:
    # Non-vacuity: a missing directory has no violations, so the check above could
    # pass on an empty tree. These four exist from PR 4 on.
    for rel in COMMITTED:
        path = ROOT / rel
        assert path.is_dir() and not path.is_symlink(), f"{rel} is not a plain directory"
        tracked = git(ROOT, "ls-files", "--", f"{rel}/.gitkeep").decode().strip()
        assert tracked == f"{rel}/.gitkeep", f"{rel}/.gitkeep is not tracked by git"


def test_templates_are_committed_in_both_languages() -> None:
    # Non-vacuity for data/synthetic/templates: committed templates in both languages.
    tracked = git(ROOT, "ls-files", "--", "data/synthetic/templates").decode().split()
    for lang in ("de", "en"):
        own = [rel for rel in tracked if rel.startswith(f"data/synthetic/templates/{lang}/")]
        assert len(own) >= 6, f"data/synthetic/templates/{lang}: {len(own)} tracked templates"
        assert all(rel.endswith(".txt") for rel in own), own


def test_generated_corpus_is_inert(tmp_path: Path) -> None:
    # Non-vacuity for data/synthetic/out, which git ignores: the check walks a corpus
    # generated here, finds it clean, and catches a file planted next to it.
    out = tmp_path / "data" / "synthetic" / "out"
    assert generate.main(["--out", str(out)]) == 0
    assert sorted(path.name for path in out.glob("*")) == CORPUS_FILES
    assert violations(tmp_path) == []
    _plant(tmp_path, "data/synthetic/out/loader.py")
    assert violations(tmp_path) == ["data/synthetic/out/loader.py: not a declared data extension"]


def test_corpus_output_is_git_ignored() -> None:
    # make data writes into the data directory, and nothing it writes is committed.
    assert generate.DEFAULT_OUT == ROOT / "data" / "synthetic" / "out"
    for name in CORPUS_FILES:
        rel = f"data/synthetic/out/{name}"
        assert git(ROOT, "check-ignore", "--no-index", rel).decode().strip() == rel


def _plant(root: Path, rel: str, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    path.chmod(mode)
    return path


def test_planted_violations_are_caught(tmp_path: Path) -> None:
    clean = _plant(tmp_path, "evals/cases/case.json")
    _plant(tmp_path, "redteam/attacks/.gitkeep")
    _plant(tmp_path, "data/synthetic/templates/de/kyc.txt")
    _plant(tmp_path, "evals/cases/.DS_Store")
    _plant(tmp_path, "evals/results/v1.md")
    _plant(tmp_path, "redteam/results/.gitkeep")

    _plant(tmp_path, "evals/cases/evil.py")
    _plant(tmp_path, "evals/cases/upper.PY")  # own stem: macOS file systems ignore case
    _plant(tmp_path, "evals/cases/notes.md")
    _plant(tmp_path, "evals/recorded/nested/deep.py")
    _plant(tmp_path, "redteam/recorded/conftest.json")
    _plant(tmp_path, "redteam/results/run.py")
    _plant(tmp_path, "evals/results/v1.json")
    _plant(tmp_path, "data/synthetic/out/run.json", mode=0o755)
    _plant(tmp_path, "evals/recorded/.DS_Store", mode=0o755)
    (tmp_path / "redteam/attacks/link.json").symlink_to(clean)
    (tmp_path / "redteam/attacks/dir").symlink_to(clean.parent, target_is_directory=True)
    (tmp_path / "redteam/attacks/dangling.json").symlink_to(tmp_path / "missing")

    assert sorted(violations(tmp_path)) == [
        "data/synthetic/out/run.json: executable",
        "evals/cases/evil.py: not a declared data extension",
        "evals/cases/notes.md: not a declared data extension",
        "evals/cases/upper.PY: not a declared data extension",
        "evals/recorded/.DS_Store: executable",
        "evals/recorded/nested/deep.py: not a declared data extension",
        "evals/results/v1.json: not a declared data extension",
        "redteam/attacks/dangling.json: symlink",
        "redteam/attacks/dir: symlink",
        "redteam/attacks/link.json: symlink",
        "redteam/recorded/conftest.json: conftest",
        "redteam/results/run.py: not a declared data extension",
    ]


def test_declared_path_that_is_not_a_plain_directory(tmp_path: Path) -> None:
    _plant(tmp_path, "evals/cases")  # a file where a directory is declared
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "redteam").mkdir()
    (tmp_path / "redteam/attacks").symlink_to(target, target_is_directory=True)
    assert sorted(violations(tmp_path)) == [
        "evals/cases: not a directory",
        "redteam/attacks: symlink",
    ]


def test_symlinked_parent_of_a_data_dir(tmp_path: Path) -> None:
    _plant(tmp_path, "outside/synthetic/out/run.json")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "data").symlink_to(tmp_path / "outside", target_is_directory=True)
    assert violations(root) == ["data: symlink"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_directory_is_reported(tmp_path: Path) -> None:
    locked = tmp_path / "evals/cases/locked"
    locked.mkdir(parents=True)
    _plant(tmp_path, "evals/cases/locked/hidden.py")
    locked.chmod(0o000)
    try:
        found = violations(tmp_path)
    finally:
        locked.chmod(0o755)
    assert found == ["evals/cases/locked: unreadable"]
