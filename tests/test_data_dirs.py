"""The data directories are inert: data extensions only, no code, no executable bit."""

from __future__ import annotations

from pathlib import Path

from data_dirs import DATA_DIRS, DATA_SUFFIXES, violations
from repo_files import ROOT


def test_data_dirs_are_declared() -> None:
    expected = {"evals/cases", "evals/recorded", "redteam/attacks", "redteam/recorded"}
    expected |= {"data/synthetic/templates", "data/synthetic/out"}
    assert set(DATA_DIRS) == expected, f"declared data dirs: {DATA_DIRS}"
    assert {".json", ".txt"} <= DATA_SUFFIXES, f"declared suffixes: {sorted(DATA_SUFFIXES)}"
    assert not {".py", ".pyc", ".pth", ".so", ".sh"} & DATA_SUFFIXES


def test_data_dirs_are_inert() -> None:
    # In PR 1 none of these directories exists yet: PR 4 adds evals/ and redteam/,
    # PR 8 adds data/synthetic/. Until then this pins the empty state.
    assert not violations(ROOT), "\n".join(violations(ROOT))


def test_planted_violations_are_caught(tmp_path: Path) -> None:
    def plant(rel: str, mode: int = 0o644) -> Path:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        path.chmod(mode)
        return path

    clean = plant("evals/cases/case.json")
    plant("redteam/attacks/.gitkeep")
    plant("data/synthetic/templates/de/kyc.txt")
    plant("evals/cases/.DS_Store")

    plant("evals/cases/evil.py")
    plant("evals/recorded/nested/deep.py")
    plant("redteam/recorded/conftest.json")
    plant("data/synthetic/out/run.json", mode=0o755)
    (tmp_path / "redteam/attacks/link.json").symlink_to(clean)

    assert sorted(violations(tmp_path)) == [
        "data/synthetic/out/run.json: executable",
        "evals/cases/evil.py: not a declared data extension",
        "evals/recorded/nested/deep.py: not a declared data extension",
        "redteam/attacks/link.json: symlink",
        "redteam/recorded/conftest.json: conftest",
    ]
