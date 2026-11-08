"""Scaffolding (M1 PR 4, the tests-first commit): the names the tests import, nothing more.

The graph check replaces this file in the next commit.
"""

from __future__ import annotations

__all__ = ("ALLOWED_DOMAINS", "graph_problems")

ALLOWED_DOMAINS: frozenset[str] = frozenset()
MAX_DEPTH = 0


def graph_problems(model: bytes) -> list[str]:
    raise NotImplementedError("M1 PR 4")
