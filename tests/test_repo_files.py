"""The shared file lister: independent of the caller's git environment, loud on failure."""

from __future__ import annotations

from pathlib import Path

import pytest
import repo_files


def test_git_environment_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Inside a git hook, GIT_DIR and GIT_INDEX_FILE point at the outer repository.
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_files.git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere" / "index"))
    assert repo_files.list_files(repo) == ["a.txt"]


def test_git_failure_shows_stderr(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="not a git repository"):
        repo_files.list_files(tmp_path)


def test_read_text(tmp_path: Path) -> None:
    (tmp_path / "bytes.bin").write_bytes(b"ok \xff end")
    (tmp_path / "link").symlink_to("target/path")
    assert repo_files.read_text(tmp_path, "bytes.bin") == "ok � end"
    assert repo_files.read_text(tmp_path, "link") == "target/path"
    assert repo_files.read_text(tmp_path, "gone.txt") is None
