"""Detectors and figure checks for the synthetic corpus: inert scaffolding (M0 PR 8, red).

Every detector finds nothing and every check reports nothing yet.
"""

from __future__ import annotations

from decimal import Decimal


def ibans(text: str) -> list[str]:
    return []


def phones(text: str) -> list[str]:
    return []


def streets(text: str) -> list[str]:
    return []


def foreign_hosts(text: str) -> list[str]:
    return []


def figures(text: str) -> list[tuple[str, Decimal]]:
    return []


def report_problems(body: str, lang: str) -> list[str]:
    return []


def figure_problems(report: str, note: str) -> list[str]:
    return []
