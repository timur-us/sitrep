"""The data directories are inert: data extensions only, no code, no executable bit."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from data_dirs import DATA_DIRS, DATA_SUFFIXES, violations
from repo_files import ROOT


def test_data_dirs_are_declared() -> None:
    expected = {"evals/cases", "evals/recorded", "redteam/attacks", "redteam/recorded"}
    expected |= {"data/synthetic/templates", "data/synthetic/out"}
    assert set(DATA_DIRS) == expected, f"declared data dirs: {DATA_DIRS}"
    assert DATA_SUFFIXES == {".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt"}, (
        f"declared suffixes: {sorted(DATA_SUFFIXES)}"
    )


def test_data_dirs_are_inert() -> None:
    # In PR 1 none of these directories exists yet: PR 4 adds evals/ and redteam/,
    # PR 8 adds data/synthetic/. Until then this pins the empty state.
    assert not violations(ROOT), "\n".join(violations(ROOT))


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

    _plant(tmp_path, "evals/cases/evil.py")
    _plant(tmp_path, "evals/cases/upper.PY")  # own stem: macOS file systems ignore case
    _plant(tmp_path, "evals/cases/notes.md")
    _plant(tmp_path, "evals/recorded/nested/deep.py")
    _plant(tmp_path, "redteam/recorded/conftest.json")
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
        "redteam/attacks/dangling.json: symlink",
        "redteam/attacks/dir: symlink",
        "redteam/attacks/link.json: symlink",
        "redteam/recorded/conftest.json: conftest",
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
