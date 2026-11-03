"""The approval gate. Scaffolding: the result type only."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    tool: str
