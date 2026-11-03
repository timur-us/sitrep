"""The output policy: what may leave the model, checked by the server (threat-model path 1).

The Protocol below is the M2 contract (M1 runs without it, to measure the unhardened
attack success rate first, as the brief plans); M0 declares it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.harness.tools import ValidCall


@dataclass(frozen=True, slots=True)
class Decision:
    """allowed, and the ids of the rules that decided; never the text they read."""

    allowed: bool
    rules: tuple[str, ...]


class OutputPolicy(Protocol):
    """M2 contract:

    - check_answer() runs on every answer before the UI sees it; check_arguments() runs
      on every validated tool call before the approval gate and before any handler.
    - Links and images are allowed only to hosts on an allowlist built from
      app/config/branding.py, of domains with no redirect or proxy endpoint (the EchoLeak
      lesson in the threat model). Recipients of draft_email only from the recipient
      allowlist.
    - The canary scan runs over answers and over every argument value; a hit blocks the
      output and raises the canary alarm in the trace.
    - A refusal is a Decision with allowed=False and the rule ids; the trace records the
      ids, never the text (CLAUDE.md rule 10).
    - The policy is code the server runs; nothing depends on the system prompt staying
      secret (Kerckhoffs).
    """

    def check_answer(self, text: str) -> Decision: ...

    def check_arguments(self, call: ValidCall) -> Decision: ...
