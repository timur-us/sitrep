"""The approval gate: side effects only after the advisor approves (threat-model path 4).

A DRAFT or SIDE_EFFECT tool (app/harness/tools.py) never runs on the model's word. The
model's call becomes a Proposal, which is all the model gets back; the advisor sees the
proposal in the UI and approves it there; only then does the server run the call. The
Protocol below is the M1 contract; M0 declares it and the result type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.auth.identity import VerifiedAdvisor
from app.harness.tools import ValidCall


@dataclass(frozen=True, slots=True)
class Proposal:
    """What a DRAFT or SIDE_EFFECT tool returns to the model: a pending approval's id,
    and nothing that could approve it."""

    proposal_id: str
    tool: str


class ApprovalRefused(Exception):
    """A token that does not approve this call, for this advisor, now. Fail closed."""


class ApprovalGate(Protocol):
    """M1 contract (the server is the token issuer, the threat model's trusted party):

    - propose() stores the validated call for the advisor and returns a Proposal. The
      proposal id is random and approves nothing on its own.
    - approve() is reachable only from the advisor's own UI session (a route with the
      verified login, CSRF-protected), never from a tool or the model. It issues a token
      that the server signs, bound to the advisor, the tool, a hash of the canonical
      arguments and an expiry; the token goes to the UI, never into the model's context.
    - redeem() runs before the handler. It checks the signature, the binding (same
      advisor, same tool, same arguments) and the expiry, and marks the token used, in
      one transaction: a token is redeemed at most once. Anything else raises
      ApprovalRefused, and the call does not run.
    - The model cannot mint, see or replay a token: nothing it writes is ever read as one.
    - The audit trail records proposal, approval and redemption by id and hash, never by
      argument text (CLAUDE.md rule 10).
    """

    def propose(self, advisor: VerifiedAdvisor, call: ValidCall) -> Proposal: ...

    def approve(self, advisor: VerifiedAdvisor, proposal_id: str) -> str: ...

    def redeem(self, advisor: VerifiedAdvisor, call: ValidCall, token: str) -> None: ...
