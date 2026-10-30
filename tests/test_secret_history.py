"""No secret in any line the branch's commits add, nor in their messages.

tests/test_no_secrets.py scans the tree only: a key added in one commit and removed in
the next passes it, but the pull request's commits still carry the key. This scan reads
the commits between the merge base with main and HEAD. In CI the lint-test job checks
out the full history for it (`fetch-depth: 0`). Findings name commit, path, line and
pattern, never the text and never a fingerprint.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import secret_history
from repo_files import ROOT, git
from secret_patterns import ALLOWLIST, PATTERNS, find_secrets, fingerprint

HOOK_TESTS = "tests/guardrails/test_block_secrets.py"


def _join(*pieces: str) -> str:
    return "".join(pieces)


FAKE_KEY = _join("sk", "-ant-", "api03-", "Q" * 24)
OTHER_KEY = _join("gh", "p_", "z" * 36)


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    """Write (or, for None, delete) the files, then commit everything."""
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@test.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    _commit(path, "base", {"README.txt": "base\n"})
    git(path, "checkout", "-q", "-b", "feature")
    return path


def test_key_added_then_removed_is_reported(repo: Path) -> None:
    added = _commit(repo, "add", {"app/x.py": f"a = 1\nb = 2\nKEY = '{FAKE_KEY}'\n"})
    _commit(repo, "remove", {"app/x.py": "a = 1\nb = 2\n"})
    # The tree is clean again: the tree scan alone would pass.
    assert find_secrets((repo / "app/x.py").read_text()) == []

    base, head = secret_history.branch_range(repo)
    assert len(secret_history.commits(repo, base, head)) == 2
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [f"{added[:12]}:app/x.py:3: anthropic-key"], lines

    shown = "\n".join(lines)
    assert FAKE_KEY not in shown and FAKE_KEY[:12] not in shown, "the report shows the key"
    assert fingerprint(FAKE_KEY) not in shown, "the report shows a fingerprint"


def test_key_in_a_commit_message_is_reported(repo: Path) -> None:
    sha = _commit(repo, f"wip\n\ntoken {OTHER_KEY}", {"a.txt": "a\n"})
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [f"{sha[:12]}:<message>:3: github-token"], lines


def test_deleted_and_renamed_files(repo: Path) -> None:
    added = _commit(repo, "add", {"docs/a.txt": f"{OTHER_KEY}\n", "keep.txt": "k\n"})
    moved = _commit(repo, "move", {"docs/a.txt": None, "docs/b.txt": f"{OTHER_KEY}\n"})
    _commit(repo, "drop", {"docs/b.txt": None})
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    # A rename counts as new lines, so the move is reported too; the deletion adds nothing.
    assert lines == [
        f"{added[:12]}:docs/a.txt:1: github-token",
        f"{moved[:12]}:docs/b.txt:1: github-token",
    ], lines


def test_the_allowed_fake_key_stays_allowed(repo: Path) -> None:
    text = (ROOT / HOOK_TESTS).read_text(encoding="utf-8")
    allowed = {pair for pair in ALLOWLIST[HOOK_TESTS].fingerprints}
    fakes = [
        match.group(0)
        for match in PATTERNS["anthropic-key"].finditer(text)
        if ("anthropic-key", fingerprint(match.group(0))) in allowed
    ]
    assert fakes, "the allowed fake key is not in the hook tests"
    _commit(repo, "hook tests", {HOOK_TESTS: f"x = '{fakes[0]}'\n"})
    other = _commit(
        repo, "another key there", {HOOK_TESTS: f"x = '{fakes[0]}'\ny = '{FAKE_KEY}'\n"}
    )
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [f"{other[:12]}:{HOOK_TESTS}:2: anthropic-key"], lines


def test_the_allowed_fake_key_is_allowed_at_its_path_only(repo: Path) -> None:
    text = (ROOT / HOOK_TESTS).read_text(encoding="utf-8")
    allowed = ALLOWLIST[HOOK_TESTS].fingerprints
    fake = next(
        match.group(0)
        for match in PATTERNS["anthropic-key"].finditer(text)
        if ("anthropic-key", fingerprint(match.group(0))) in allowed
    )
    sha = _commit(repo, "copied", {"tests/elsewhere.py": f"x = '{fake}'\n"})
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [f"{sha[:12]}:tests/elsewhere.py:1: anthropic-key"], lines


@pytest.mark.parametrize(
    ("files", "path"),
    [
        ({"blob.bin": "\0\0header\nKEY\n"}, "blob.bin"),  # a NUL makes git call it binary
        ({".gitattributes": "*.dat -diff\n", "notes.dat": "KEY\n"}, "notes.dat"),
    ],
)
def test_binary_files_are_scanned(repo: Path, files: dict[str, str], path: str) -> None:
    files = {rel: text.replace("KEY", OTHER_KEY) for rel, text in files.items()}
    sha = _commit(repo, "binary", files)
    _commit(repo, "gone", {path: None})
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert f"{sha[:12]}:{path}:2:" in "\n".join(lines), lines


def _git_unchecked(repo: Path, *args: str) -> None:
    """A git command that may fail on purpose (a merge with a conflict)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, check=False)


def test_a_key_added_while_resolving_a_merge_is_reported(repo: Path) -> None:
    # A strict ruleset makes "update branch" merges routine; their resolutions add lines.
    _commit(repo, "feature side", {"f.txt": "feature\n"})
    git(repo, "checkout", "-q", "main")
    _commit(repo, "main side", {"f.txt": "main\n"})
    git(repo, "checkout", "-q", "feature")
    _git_unchecked(repo, "-c", "core.hooksPath=/dev/null", "merge", "-q", "main")
    merge = _commit(repo, "merge main", {"f.txt": f"resolved\n{OTHER_KEY}\n"})
    assert len(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3, "no merge"
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [f"{merge[:12]}:f.txt:2: github-token"], lines


def test_a_key_in_a_path_is_reported_without_the_path(repo: Path) -> None:
    sha = _commit(repo, "named", {f"{OTHER_KEY}.txt": f"{FAKE_KEY}\n"})
    base, head = secret_history.branch_range(repo)
    lines = secret_history.report(secret_history.scan(repo, base, head))
    assert lines == [
        f"{sha[:12]}:<path>:0: github-token",
        f"{sha[:12]}:<path>:1: anthropic-key",
    ], lines
    assert OTHER_KEY not in "\n".join(lines)


def test_merge_base_is_the_start(repo: Path) -> None:
    # Commits already on main are not the branch's: they are out of range.
    git(repo, "checkout", "-q", "main")
    _commit(repo, "on main", {"main.txt": f"{OTHER_KEY}\n"})
    git(repo, "checkout", "-q", "feature")
    _commit(repo, "on feature", {"f.txt": "f\n"})
    base, head = secret_history.branch_range(repo)
    assert len(secret_history.commits(repo, base, head)) == 1
    assert secret_history.scan(repo, base, head) == []


def test_shallow_clone_fails_loudly(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "more", {"b.txt": "b\n"})
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "feature", repo.as_uri(), str(clone))
    with pytest.raises(AssertionError, match="shallow"):
        secret_history.branch_range(clone)


def test_this_branch_adds_no_secret() -> None:
    base, head = secret_history.branch_range(ROOT)
    shas = secret_history.commits(ROOT, base, head)
    head_sha = git(ROOT, "rev-parse", "HEAD").decode().strip()
    # Non-vacuity: off main, the range holds the branch's commits. On main (a push run)
    # the range is empty by design: the pull request's run already read those commits.
    if base != head_sha:
        assert shas, f"no commits between {base[:12]} and HEAD"
    lines = secret_history.report(secret_history.scan(ROOT, base, head))
    assert not lines, "possible secrets in the branch's commits (text not shown):\n" + "\n".join(
        lines
    )
