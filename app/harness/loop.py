"""The agent loop (ADR 0001; M1 plan, PR 10). Scaffolding: built next."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.auth.identity import VerifiedAdvisor
from app.harness.context import Untrusted
from app.harness.grounding import GroundingReport
from app.harness.model import ModelClient
from app.harness.tools import ToolRegistry

MAX_TURNS = 6
MAX_TOKENS = 8_192
REQUEST_BUDGET = 150_000
CHARS_PER_TOKEN = 3
MAX_QUESTION = 2_000
MAX_MANDATE_ID = 128

Stop = Literal["end_turn", "refusal", "budget", "max_turns"]


class Handler(Protocol):
    """A READ tool's handler: the verified advisor and the validated arguments in, an
    Untrusted result out."""

    def __call__(self, advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]: ...


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LoopResult:
    answer: str
    citations: tuple[str, ...]
    grounding: GroundingReport
    usage: Usage
    cost_usd: Decimal
    stop: Stop
    turns: int
    trace_id: str | None


class LoopError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def read_registry() -> ToolRegistry:
    raise NotImplementedError("M1 PR 10")


def run(
    advisor: VerifiedAdvisor,
    question: str,
    mandate_id: str | None = None,
    *,
    client: ModelClient,
    handlers: Mapping[str, Handler],
    model: str,
) -> LoopResult:
    raise NotImplementedError("M1 PR 10")
