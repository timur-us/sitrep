"""The four MVP tools of docs/brief.md, declared only: the handlers are M1's.

| Tool | Effect | Approval |
|---|---|---|
| search_client_docs | READ | no |
| get_portfolio | READ | no |
| draft_email | DRAFT | yes |
| create_followup | SIDE_EFFECT | yes |

All four touch client data, so all four are CONFIDENTIAL. The READ tools return
Untrusted results; the other two return a Proposal, which approves nothing
(app/harness/approval.py). No argument names an advisor: whose mandates a call reaches
is decided by the login and row-level security. M1's handlers must therefore look up a
mandate id the model names only inside the advisor transaction (app/db/session.py),
where another advisor's mandate does not exist; M1 tests that through these tools. Each
argument has bounds; the canary scan and the recipient allowlist are M2's
(app/harness/policy.py).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.harness.approval import Proposal
from app.harness.context import Untrusted
from app.harness.tools import DataClass, Effect, ToolRegistry, ToolSpec

MandateId = Annotated[str, Field(min_length=1, max_length=128)]
Address = Annotated[str, Field(min_length=3, max_length=254)]


class Arguments(BaseModel):
    """Base of every tool's arguments: no extra keys, nothing changed after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchClientDocs(Arguments):
    query: str = Field(min_length=1, max_length=500, description="What to look for.")
    mandate_id: MandateId | None = Field(
        default=None, description="Search one mandate only; all of the advisor's otherwise."
    )
    limit: int = Field(default=8, ge=1, le=20, description="At most this many hits.")


class GetPortfolio(Arguments):
    mandate_id: MandateId


class DraftEmail(Arguments):
    mandate_id: MandateId
    # A tuple, so a validated call cannot gain a recipient (review finding sec-5).
    to: tuple[Address, ...] = Field(min_length=1, max_length=10)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)


class CreateFollowup(Arguments):
    mandate_id: MandateId
    title: str = Field(min_length=1, max_length=200)
    due: datetime.date
    notes: str = Field(default="", max_length=2_000)


@dataclass(frozen=True)
class DocumentHit:
    document_id: str
    mandate_id: str
    title: str
    lang: str
    excerpt: str


@dataclass(frozen=True)
class Portfolio:
    mandate_id: str
    document_id: str
    as_of: datetime.date
    report: str


TOOLS = (
    ToolSpec(
        name="search_client_docs",
        description="Search the documents of the advisor's own mandates.",
        arguments=SearchClientDocs,
        result=Untrusted[list[DocumentHit]],
        effect=Effect.READ,
        data_class=DataClass.CONFIDENTIAL,
        requires_approval=False,
    ),
    ToolSpec(
        name="get_portfolio",
        description="The latest portfolio report of one of the advisor's mandates.",
        arguments=GetPortfolio,
        result=Untrusted[Portfolio],
        effect=Effect.READ,
        data_class=DataClass.CONFIDENTIAL,
        requires_approval=False,
    ),
    ToolSpec(
        name="draft_email",
        description="Propose an e-mail to the client; the advisor approves it before it exists.",
        arguments=DraftEmail,
        result=Proposal,
        effect=Effect.DRAFT,
        data_class=DataClass.CONFIDENTIAL,
        requires_approval=True,
    ),
    ToolSpec(
        name="create_followup",
        description="Propose a follow-up task; the advisor approves it before it is created.",
        arguments=CreateFollowup,
        result=Proposal,
        effect=Effect.SIDE_EFFECT,
        data_class=DataClass.CONFIDENTIAL,
        requires_approval=True,
    ),
)


def build_registry() -> ToolRegistry:
    """A registry with the four tools, each admitted by register()'s checks."""
    registry = ToolRegistry()
    for spec in TOOLS:
        registry.register(spec)
    return registry
