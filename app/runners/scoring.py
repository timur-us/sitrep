"""Scaffolding (M1 PR 16, commit 1): the eval scorers, built in the next commit."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

SCORERS = ("answered", "citations", "grounding", "tools", "no_foreign_record", "refusal", "gap")
HARD = frozenset({"no_foreign_record"})


@dataclass(frozen=True)
class Expect:
    stop: tuple[str, ...] = ("end_turn",)
    tools: tuple[str, ...] = ()
    cites: bool = False
    refusal: bool = False
    gap: bool = False
    past_dates: tuple[datetime.date, ...] = ()
    invented: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    lang: str
    advisor_id: str
    question: str
    expect: Expect


@dataclass(frozen=True)
class Score:
    failed: tuple[str, ...]


def _todo(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("M1 PR 16")


parse_case = owners = integrity = score = _todo
