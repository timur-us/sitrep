"""The number guard and the citation check (M1 plan, PR 9). Scaffolding: built next."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.harness.context import Untrusted

CITATION_FORM = "[source: <record id>]"


@dataclass(frozen=True, slots=True)
class GroundingReport:
    figures: int
    unmatched: int
    citations: tuple[str, ...]
    unknown_citations: int


def check(answer: str, sources: Sequence[Untrusted[Any]]) -> GroundingReport:
    raise NotImplementedError("M1 PR 9")
