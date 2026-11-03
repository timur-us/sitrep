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
    - Proposals are stored with their advisor, under row-level security like every
      other advisor-scoped row; approve() and redeem() refuse another advisor's proposal.
    - approve() is reachable only from the advisor's own UI session (a route with the
      verified login, CSRF-protected), never from a tool or the model. The UI shows the
      stored arguments as they will run (every recipient, escaped), not the model's
      summary. approve() issues a token bound to the advisor, the proposal id, the tool,
      a hash of the canonical arguments and an expiry; the token goes to the UI, never
      into the model's context.
    - redeem() runs before the handler. It checks the binding (same advisor, same
      proposal, same tool, same arguments) and the expiry, and marks the token used, in
      one transaction: a token is redeemed at most once. What runs is the stored call,
      never arguments supplied with the token. Anything else raises ApprovalRefused, and
      the call does not run.
    - Open for M1's ADR (review finding sec-4): a signed token needs a signing key, a
      second secret beside the API key (threat model, "Assets"); an opaque random token
      stored as a hash needs none. The signature of redeem() below may change with it,
      to take the proposal id and return the stored ValidCall.
    - The model cannot mint, see or replay a token: nothing it writes is ever read as one.
    - The audit trail records proposal, approval and redemption by id and hash, never by
      argument text (CLAUDE.md rule 10).
    """

    def propose(self, advisor: VerifiedAdvisor, call: ValidCall) -> Proposal: ...

    def approve(self, advisor: VerifiedAdvisor, proposal_id: str) -> str: ...

    def redeem(self, advisor: VerifiedAdvisor, call: ValidCall, token: str) -> None: ...
