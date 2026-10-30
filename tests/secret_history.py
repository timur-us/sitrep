"""Secret scan over the branch's commits (scaffold, PR 4 commit 1)."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class Finding(NamedTuple):
    commit: str
    path: str
    line: int
    pattern: str
    fingerprint: str


def branch_range(root: Path) -> tuple[str, str]:
    return "HEAD", "HEAD"


def commits(root: Path, base: str, head: str) -> list[str]:
    return []


def scan(root: Path, base: str, head: str) -> list[Finding]:
    return []


def report(findings: list[Finding]) -> list[str]:
    return []
