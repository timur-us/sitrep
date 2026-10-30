"""Secret scan over the branch's commits: every line they add, and their messages.

The tree scan (tests/test_no_secrets.py) misses a key that one commit adds and a later
one removes; the pull request still carries it. This scan reads the commits between
the merge base with main (origin/main, else main) and HEAD. Each commit's patch is
read as text (`--text`: binary files and `-diff` attributes do not hide lines), with
renames off, so a moved file counts as added lines, and with no external diff or text
conversion. A merge is read as a remerge diff: the lines its conflict resolution adds,
not the lines it brings in from main. The PR 1 patterns and allowlist apply, to the
lines, to each new path and to the commit message. On main (a push run) the range is
empty by design: the pull request's run read those commits.

A finding is "<commit>:<path>:<line>: <pattern>", with `<message>` for a commit message
and `<path>` for a path that itself matches a pattern (line 0 is the path). It never
carries the matched text or a fingerprint. A finding means rotating the key: the
commits stay reachable on GitHub through the pull request's refs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from repo_files import git
from secret_patterns import ALLOWLIST, find_secrets

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MESSAGE = "<message>"


class Finding(NamedTuple):
    commit: str
    path: str
    line: int
    pattern: str
    fingerprint: str  # for the allowlist only; never printed


def branch_range(root: Path) -> tuple[str, str]:
    """(merge base with main, HEAD) as commit ids. A shallow clone fails: its range
    would be cut short without a word."""
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise AssertionError(
            "shallow clone: the history scan needs every commit (CI: fetch-depth: 0)"
        )
    refs = git(
        root, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/main", "refs/heads/main"
    )
    names = refs.decode().split()
    if not names:
        raise AssertionError("neither origin/main nor main exists: no merge base to start from")
    main = "refs/remotes/origin/main" if "refs/remotes/origin/main" in names else names[0]
    base = git(root, "merge-base", main, "HEAD")
    head = git(root, "rev-parse", "HEAD")
    return base.decode().strip(), head.decode().strip()


def commits(root: Path, base: str, head: str) -> list[str]:
    out = git(root, "rev-list", "--reverse", f"{base}..{head}")
    return out.decode().split()


def added_lines(root: Path, commit: str) -> Iterator[tuple[str, int, str]]:
    """(path, line in the new file, text) for every line the commit adds."""
    patch = git(
        root,
        "-c",
        "core.quotepath=off",
        "show",
        "--format=",
        "--patch",
        "--text",
        "--remerge-diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        commit,
    ).decode("utf-8", errors="replace")
    path = ""
    number = 0
    in_hunk = False
    for raw in patch.split("\n"):
        if raw.startswith("diff --git "):
            path, in_hunk = "", False
            continue
        if match := HUNK.match(raw):
            number, in_hunk = int(match.group(1)), True
            continue
        if not in_hunk:
            if raw.startswith("+++ "):
                target = raw[4:].strip('"')
                path = target.removeprefix("b/")
            continue
        if raw.startswith("+"):
            yield path, number, raw[1:]
            number += 1


def scan(root: Path, base: str, head: str) -> list[Finding]:
    found: list[Finding] = []
    for commit in commits(root, base, head):
        seen_paths: set[str] = set()
        for path, number, text in added_lines(root, commit):
            shown = "<path>" if find_secrets(path) else path
            if path not in seen_paths:
                seen_paths.add(path)
                found += [
                    Finding(commit, shown, 0, hit.pattern, hit.fingerprint)
                    for hit in find_secrets(path)
                ]
            allowed = ALLOWLIST[path].fingerprints if path in ALLOWLIST else frozenset()
            found += [
                Finding(commit, shown, number, hit.pattern, hit.fingerprint)
                for hit in find_secrets(text)
                if (hit.pattern, hit.fingerprint) not in allowed
            ]
        message = git(root, "log", "-1", "--format=%B", commit).decode("utf-8", errors="replace")
        found += [
            Finding(commit, MESSAGE, hit.line, hit.pattern, hit.fingerprint)
            for hit in find_secrets(message)
        ]
    return found


def report(findings: list[Finding]) -> list[str]:
    return [f"{f.commit[:12]}:{f.path}:{f.line}: {f.pattern}" for f in findings]
