"""The READ tools' handlers (ADR 0013): search_client_docs and get_portfolio, scoped by
row-level security (threat-model path 2).

A map from tool name to handler, kept apart from the specs (app/harness/mvp_tools.py):
read_handlers() binds the two to the app's pool and embedder. Each handler checks its
call before any work, runs in its own advisor transaction (app/db/session.py) and returns
Untrusted with Provenance(tool, record_ids), the records in order. Nothing is kept past
the block: the handle ends with it, and a result holds plain values only.

search_client_docs:
- The query is embedded first, outside the transaction, so no connection waits on the
  model. A query over the model's limit (500 characters can tokenize to more than its
  512 tokens) is refused as ToolFailed(QUERY_TOO_LONG) before a connection is taken, and
  never cut.
- With a mandate id, the mandate is looked up inside the transaction first: another
  advisor's mandate is not there, so it is "no such mandate", the same empty answer as
  an id that exists nowhere. Then only that mandate's documents are searched, and no
  product sheet.
- Without one, three ranked lists of at most CANDIDATES records each: full-text search
  in English and in German, each language's statements keeping to their own documents
  and sheets (ADR 0005), and the nearest vectors of the query's model and revision, a
  document by its nearest passage, a sheet by its one vector. Reciprocal rank fusion
  (RRF_K) joins them; ties go by kind and id, so a search is deterministic.
- A hit names its kind, its mandate (none for a sheet), its title and language, its
  trust from the record's table (never from an id), and an excerpt: the stored passage
  nearest the query, whole. A sheet is one passage. A record that a concurrent load
  replaced between two statements is left out.

get_portfolio looks the mandate up the same way, then reads its report from the
portfolio system only, so no e-mail can pose as one; None when there is no such mandate
or no report.

Every row of an advisor-scoped table carries its advisor_id, and the handlers refuse a
row of another advisor (GuardError) rather than return it: row-level security is the
boundary, this is a second line behind it. Trust is provenance, never permission: every
result is Untrusted (CLAUDE.md rule 9). The handlers log nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from app.auth.identity import VerifiedAdvisor
from app.db import queries
from app.db.session import AdvisorConnection, GuardError, Pool, advisor_transaction
from app.embeddings.base import Embedder, TextTooLong
from app.harness.context import Provenance, Untrusted
from app.harness.mvp_tools import DocumentHit, GetPortfolio, Portfolio, SearchClientDocs

SEARCH = "search_client_docs"
PORTFOLIO = "get_portfolio"
DOCUMENT = "document"
PRODUCT_SHEET = "product_sheet"
KINDS = (DOCUMENT, PRODUCT_SHEET)
# Reciprocal rank fusion's constant (Cormack, Clarke and Buettcher, SIGIR 2009).
RRF_K = 60
# Records per ranked list, the most a search may ask for (SearchClientDocs.limit); the
# statements in app/db/queries.py say LIMIT 20.
CANDIDATES = 20
QUERY_TOO_LONG = "query_too_long"
FOREIGN_ROW = "a row of another advisor reached a READ handler"

Key = tuple[str, str]  # (kind, record id)


class ToolFailed(Exception):
    """A READ call the handler refuses. kind is a fixed word the loop may give the model
    (M1 PR 10); no input is in it."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def fuse(ranked: Sequence[Sequence[Key]], k: int = RRF_K) -> list[Key]:
    """Reciprocal rank fusion: each list adds 1 / (k + rank) to every key it holds, once,
    at its best rank. The keys by score, best first, ties by key."""
    scores: dict[Key, float] = {}
    for keys in ranked:
        for rank, key in enumerate(dict.fromkeys(keys), start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))


def ranked(rows: Iterable[tuple[Key, float]], *, best: str) -> list[Key]:
    """The keys best first by their score, "high" for a full-text rank and "low" for a
    distance: each once at its best, ties by key, at most CANDIDATES."""
    if best not in ("high", "low"):
        raise ValueError("best is high or low")
    sign = -1.0 if best == "high" else 1.0
    order = sorted(rows, key=lambda row: (sign * row[1], row[0]))
    return list(dict.fromkeys(key for key, _ in order))[:CANDIDATES]


def vector_text(values: Sequence[float]) -> str:
    """values in pgvector's text form, as app/db/load.py writes them."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def _own(advisor_id: str, rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """rows, refused unless each one's first column, its advisor_id, is advisor_id's."""
    if any(row[0] != advisor_id for row in rows):
        raise GuardError(FOREIGN_ROW)
    return rows


def _visible(conn: AdvisorConnection, advisor_id: str, mandate_id: str) -> bool:
    """Whether the advisor of the transaction has this mandate."""
    rows = conn.execute(queries.MANDATE_VISIBLE, (mandate_id,)).fetchall()
    return bool(_own(advisor_id, rows))


def nearest_chunks(
    conn: AdvisorConnection,
    advisor_id: str,
    vector: str,
    model: str,
    revision: str,
    mandate_id: str | None = None,
) -> list[tuple[str, float]]:
    """The stored passages nearest vector, as (document id, distance), nearest first: at
    most CANDIDATES, of model and revision only, of mandate_id's documents if given."""
    if mandate_id is None:
        rows = conn.execute(queries.NEAREST_CHUNKS, (vector, model, revision)).fetchall()
    else:
        rows = conn.execute(
            queries.NEAREST_MANDATE_CHUNKS, (vector, mandate_id, model, revision)
        ).fetchall()
    return [(document, distance) for _, document, distance in _own(advisor_id, rows)]


def search(
    conn: AdvisorConnection,
    advisor_id: str,
    arguments: SearchClientDocs,
    vector: str,
    model: str,
    revision: str,
) -> list[DocumentHit]:
    """search_client_docs inside an advisor transaction, the query already embedded."""
    mandate, query = arguments.mandate_id, arguments.query
    sheets: dict[str, list[tuple[Any, ...]]] = {"english": [], "german": []}
    if mandate is None:
        documents = {
            "english": conn.execute(queries.SEARCH_DOCUMENTS_ENGLISH, (query,)).fetchall(),
            "german": conn.execute(queries.SEARCH_DOCUMENTS_GERMAN, (query,)).fetchall(),
        }
        sheets = {
            "english": conn.execute(queries.SEARCH_SHEETS_ENGLISH, (query,)).fetchall(),
            "german": conn.execute(queries.SEARCH_SHEETS_GERMAN, (query,)).fetchall(),
        }
        sheet_vectors = conn.execute(queries.NEAREST_SHEETS, (vector, model, revision)).fetchall()
    elif _visible(conn, advisor_id, mandate):
        documents = {
            "english": conn.execute(
                queries.SEARCH_MANDATE_DOCUMENTS_ENGLISH, (query, mandate)
            ).fetchall(),
            "german": conn.execute(
                queries.SEARCH_MANDATE_DOCUMENTS_GERMAN, (query, mandate)
            ).fetchall(),
        }
        sheet_vectors = []
    else:
        return []
    lists = []
    for lang in ("english", "german"):
        rows = [((DOCUMENT, record), rank) for _, record, rank in _own(advisor_id, documents[lang])]
        rows += [((PRODUCT_SHEET, record), rank) for record, rank in sheets[lang]]
        lists.append(ranked(rows, best="high"))
    near = [
        ((DOCUMENT, document), distance)
        for document, distance in nearest_chunks(conn, advisor_id, vector, model, revision, mandate)
    ]
    near += [((PRODUCT_SHEET, record), distance) for record, distance in sheet_vectors]
    lists.append(ranked(near, best="low"))
    return _hits(conn, advisor_id, fuse(lists)[: arguments.limit], vector, model, revision)


def _hits(
    conn: AdvisorConnection,
    advisor_id: str,
    keys: Sequence[Key],
    vector: str,
    model: str,
    revision: str,
) -> list[DocumentHit]:
    """The hits for the fused keys, in their order."""
    documents = [record for kind, record in keys if kind == DOCUMENT]
    sheets = [record for kind, record in keys if kind == PRODUCT_SHEET]
    details: dict[str, tuple[Any, ...]] = {}
    sheet_rows: dict[str, tuple[Any, ...]] = {}
    if documents:
        # Details and the nearest passage in one statement, so one snapshot: a trust
        # never pairs with another load's passage (security review sec-2).
        rows = conn.execute(queries.DOCUMENT_HITS, (model, revision, vector, documents)).fetchall()
        details = {row[1]: row for row in _own(advisor_id, rows)}
    if sheets:
        rows = conn.execute(queries.SHEET_DETAILS, (sheets,)).fetchall()
        sheet_rows = {row[0]: row for row in rows}
    hits = []
    for kind, record in keys:
        if kind == DOCUMENT and record in details:
            _, _, mandate_id, title, lang, trust, passage = details[record]
            hits.append(
                DocumentHit(
                    kind=DOCUMENT,
                    document_id=record,
                    mandate_id=mandate_id,
                    title=title,
                    lang=lang,
                    trust=trust,
                    excerpt=passage or "",
                )
            )
        elif kind == PRODUCT_SHEET and record in sheet_rows:
            _, title, lang, trust, body = sheet_rows[record]
            hits.append(
                DocumentHit(
                    kind=PRODUCT_SHEET,
                    document_id=record,
                    mandate_id=None,
                    title=title,
                    lang=lang,
                    trust=trust,
                    excerpt=body,
                )
            )
    return hits


def portfolio(conn: AdvisorConnection, advisor_id: str, mandate_id: str) -> Portfolio | None:
    """get_portfolio inside an advisor transaction."""
    if not _visible(conn, advisor_id, mandate_id):
        return None
    rows = _own(advisor_id, conn.execute(queries.PORTFOLIO_REPORT, (mandate_id,)).fetchall())
    if not rows:
        return None
    _, record, mandate, title, lang, body = rows[0]
    return Portfolio(mandate_id=mandate, document_id=record, title=title, lang=lang, report=body)


def _check(advisor: object, arguments: object, model: type[BaseModel]) -> None:
    if type(advisor) is not VerifiedAdvisor:
        raise TypeError(f"a READ handler takes a VerifiedAdvisor, not {type(advisor).__name__}")
    if type(arguments) is not model:
        raise TypeError(f"{model.__name__} expected, not {type(arguments).__name__}")


def search_client_docs(
    pool: Pool, embedder: Embedder, advisor: VerifiedAdvisor, arguments: SearchClientDocs
) -> Untrusted[list[DocumentHit]]:
    _check(advisor, arguments, SearchClientDocs)
    try:
        embedded: list[float] | None = embedder.embed_query(arguments.query)
    except TextTooLong:
        embedded = None
    if embedded is None:
        # Raised outside the except: no cause and no context, whose frames hold the
        # query (security review sec-5).
        raise ToolFailed(QUERY_TOO_LONG) from None
    vector = vector_text(embedded)
    with advisor_transaction(pool, advisor) as conn:
        hits = search(
            conn, advisor.advisor_id, arguments, vector, embedder.model_id, embedder.revision
        )
    records = tuple(hit.document_id for hit in hits)
    return Untrusted(hits, Provenance(tool=SEARCH, record_ids=records))


def get_portfolio(
    pool: Pool, advisor: VerifiedAdvisor, arguments: GetPortfolio
) -> Untrusted[Portfolio | None]:
    _check(advisor, arguments, GetPortfolio)
    with advisor_transaction(pool, advisor) as conn:
        report = portfolio(conn, advisor.advisor_id, arguments.mandate_id)
    records = () if report is None else (report.document_id,)
    return Untrusted(report, Provenance(tool=PORTFOLIO, record_ids=records))


def read_handlers(
    pool: Pool, embedder: Embedder
) -> Mapping[str, Callable[[VerifiedAdvisor, BaseModel], Untrusted[Any]]]:
    """The READ tools' handlers by tool name, bound to the app's pool and embedder; the
    mapping cannot be changed."""

    def search_handler(advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        return search_client_docs(pool, embedder, advisor, arguments)  # type: ignore[arg-type]

    def portfolio_handler(advisor: VerifiedAdvisor, arguments: BaseModel) -> Untrusted[Any]:
        return get_portfolio(pool, advisor, arguments)  # type: ignore[arg-type]

    return MappingProxyType({SEARCH: search_handler, PORTFOLIO: portfolio_handler})
