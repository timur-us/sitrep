"""The four MVP tools, declared only. Scaffolding: the naive first draft."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from pydantic import BaseModel

from app.harness.approval import Proposal
from app.harness.tools import DataClass, Effect, ToolRegistry, ToolSpec


class SearchClientDocs(BaseModel):
    query: str
    advisor_id: str | None = None
    mandate_id: str | None = None
    limit: int = 8


class GetPortfolio(BaseModel):
    mandate_id: str


class DraftEmail(BaseModel):
    mandate_id: str
    to: list[str]
    subject: str
    body: str


class CreateFollowup(BaseModel):
    mandate_id: str
    title: str
    due: datetime.date
    notes: str = ""


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
        description="Search the documents of the advisor's mandates.",
        arguments=SearchClientDocs,
        result=list[DocumentHit],
        effect=Effect.READ,
        data_class=DataClass.INTERNAL,
        requires_approval=False,
    ),
    ToolSpec(
        name="get_portfolio",
        description="The latest portfolio report of one mandate.",
        arguments=GetPortfolio,
        result=Portfolio,
        effect=Effect.READ,
        data_class=DataClass.INTERNAL,
        requires_approval=False,
    ),
    ToolSpec(
        name="draft_email",
        description="Draft an e-mail to the client.",
        arguments=DraftEmail,
        result=Proposal,
        effect=Effect.READ,
        data_class=DataClass.INTERNAL,
        requires_approval=False,
    ),
    ToolSpec(
        name="create_followup",
        description="Create a follow-up task.",
        arguments=CreateFollowup,
        result=Proposal,
        effect=Effect.READ,
        data_class=DataClass.INTERNAL,
        requires_approval=False,
    ),
)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in TOOLS:
        registry.register(spec)
    return registry
