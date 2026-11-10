"""The READ tools' handlers (ADR 0013): search_client_docs and get_portfolio.

Scaffolding (M1 PR 8, commit 1): the names and constants the tests import; the bodies
come with the build.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from app.auth.identity import VerifiedAdvisor
from app.db.session import Pool
from app.embeddings.base import Embedder
from app.harness.context import Untrusted
from app.harness.mvp_tools import DocumentHit, GetPortfolio, Portfolio, SearchClientDocs

SEARCH = "search_client_docs"
PORTFOLIO = "get_portfolio"
DOCUMENT = "document"
PRODUCT_SHEET = "product_sheet"
KINDS = (DOCUMENT, PRODUCT_SHEET)
RRF_K = 60
CANDIDATES = 20
QUERY_TOO_LONG = "query_too_long"

Key = tuple[str, str]


class ToolFailed(Exception):
    """A READ call the handler refuses; kind is a fixed word, never input."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def fuse(ranked: Sequence[Sequence[Key]], k: int = RRF_K) -> list[Key]:
    raise NotImplementedError


def ranked(rows: Iterable[tuple[Key, float]], *, best: str) -> list[Key]:
    raise NotImplementedError


def vector_text(values: Sequence[float]) -> str:
    raise NotImplementedError


def search(
    conn: Any,
    advisor_id: str,
    arguments: SearchClientDocs,
    vector: str,
    model: str,
    revision: str,
) -> list[DocumentHit]:
    raise NotImplementedError


def nearest_chunks(
    conn: Any,
    advisor_id: str,
    vector: str,
    model: str,
    revision: str,
    mandate_id: str | None = None,
) -> list[tuple[str, float]]:
    raise NotImplementedError


def portfolio(conn: Any, advisor_id: str, mandate_id: str) -> Portfolio | None:
    raise NotImplementedError


def search_client_docs(
    pool: Pool, embedder: Embedder, advisor: VerifiedAdvisor, arguments: SearchClientDocs
) -> Untrusted[list[DocumentHit]]:
    raise NotImplementedError


def get_portfolio(
    pool: Pool, advisor: VerifiedAdvisor, arguments: GetPortfolio
) -> Untrusted[Portfolio | None]:
    raise NotImplementedError


def read_handlers(
    pool: Pool, embedder: Embedder
) -> Mapping[str, Callable[[VerifiedAdvisor, BaseModel], Untrusted[Any]]]:
    raise NotImplementedError
