"""Shared runner for the eval and red-team gates (scaffold, PR 4 commit 1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Suite:
    name: str
    case_dir: str


def host_problem(host: str) -> str | None:
    return None


def text_problems(text: str) -> list[str]:
    return []


def main(suite: Suite, argv: Sequence[str] | None = None) -> int:
    return 0
