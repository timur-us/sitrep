"""Retrieval through the real READ handlers (M1 PR 8; ADR 0013; threat-model path 2).

The corpus is the one `make load` writes, loaded here if the database lacks it, and every
query is embedded by the real model. Results are read the one sanctioned way, through
wrap() (app/harness/context.py); their record ids also through the provenance.

- The boundary. An advisor's searches return their own documents and the global product
  sheets, never another advisor's record, in English and German; the demo mandate's
  family, searched by the other advisors, included.
- A foreign mandate. Another advisor's mandate id gives the same answer as an id that
  exists nowhere, for both tools: an empty search and no portfolio. The advisor's own
  mandate gives exactly its documents, and its portfolio report.
- The portfolio is the mandate's portfolio_system document: an e-mail dressed as the
  report is never returned, even where it would come first by id.
- Product sheets reach all three advisors, in both languages, with no mandate.
- Trust is read from the tables, never inferred from an id.
- The iterative scan. Pushed onto the HNSW index while row-level security hides the
  nearest rows, the vector branch still fills its candidates; with the scan off
  (control) it does not.
- Clean connections. After the handlers, the next checkout reuses the same connection,
  which the guard passed, with no setting left behind.
- The index. No index on tsv can serve the app role: `@@` is not leakproof, so under
  row-level security the planner reads the advisor's rows through the primary key and
  filters them. A GIN index planted for the test is used by the superuser, and not by
  the app role, with sequential scans off for both.
- A query over the model's limit is refused before any connection is taken, and no
  query text reaches a log record.
"""

from __future__ import annotations

import html
import json
import logging
import math
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from dbtools import SUPERUSER_URL, Connection
from psycopg import sql
from psycopg_pool import ConnectionPool
from seed_rows import MINE, OTHER, SEED

from app.auth.identity import issue
from app.config import branding
from app.config.embedding import DIM, MAX_TOKENS
from app.config.settings import loopback_database_url
from app.db import load, names, queries, session
from app.db.connect import connect
from app.db.migrate import become_owner
from app.db.session import advisor_transaction
from app.embeddings.base import query_input
from app.embeddings.onnx import OnnxEmbedder
from app.harness import handlers
from app.harness.context import Untrusted, wrap
from app.harness.mvp_tools import GetPortfolio, SearchClientDocs
from data.synthetic import generate

pytestmark = pytest.mark.usefixtures("database")

SEARCH = "search_client_docs"
PORTFOLIO = "get_portfolio"
DEMO = branding.DEMO_MANDATE
FAMILY = DEMO.family
OTHERS = [person for person in branding.ADVISORS if person.id != DEMO.advisor_id]
MIGRATOR_URL = loopback_database_url(names.MIGRATOR_ROLE)

# Plain questions an advisor might ask, two per language, and the demo family's.
QUERIES = {
    "en-report": "portfolio report and risk profile",
    "en-meeting": "the client asked about the next meeting",
    "de-report": "Vermögensbericht und Risikoprofil",
    "de-meeting": "Gesprächsnotiz zum nächsten Termin",
}
FAMILY_QUERIES = {
    "en-family": f"the {FAMILY} family portfolio report",
    "de-family": f"Vermögensbericht Familie {FAMILY}",
}
EVERY_QUERY = QUERIES | FAMILY_QUERIES


def table(name: str) -> sql.Identifier:
    return sql.Identifier(names.SCHEMA, name)


def is_loaded(corpus: load.Corpus, embedder: OnnxEmbedder) -> bool:
    """Every corpus document and sheet is in the database with a vector of this model."""
    model = (embedder.model_id, embedder.revision)
    with connect(SUPERUSER_URL) as su:
        documents = set(
            su.execute(
                sql.SQL(
                    "SELECT d.advisor_id, d.id FROM {} d JOIN {} c"
                    " ON c.advisor_id = d.advisor_id AND c.document_id = d.id"
                    " WHERE c.embedding_model = %s AND c.embedding_revision = %s"
                ).format(table("documents"), table("chunks")),
                model,
            ).fetchall()
        )
        sheets = {
            row[0]
            for row in su.execute(
                sql.SQL(
                    "SELECT id FROM {} WHERE embedding_model = %s AND embedding_revision = %s"
                ).format(table("product_sheets")),
                model,
            ).fetchall()
        }
    wanted = {(document.advisor_id, document.id) for document in corpus.documents}
    return wanted <= documents and {sheet.id for sheet in corpus.sheets} <= sheets


@pytest.fixture(scope="module")
def corpus(
    database: None, embedder: OnnxEmbedder, tmp_path_factory: pytest.TempPathFactory
) -> load.Corpus:
    """The reviewed corpus, in the database: `make load`'s rows, or loaded here."""
    out: Path = tmp_path_factory.mktemp("corpus")
    assert generate.main(["--out", str(out)]) == 0
    corpus = load.read_corpus(out)
    if not is_loaded(corpus, embedder):
        with connect(MIGRATOR_URL, autocommit=True) as conn:
            become_owner(conn)
            load.write(conn, load.prepare(corpus, embedder, embedder.tokenizer))
    assert is_loaded(corpus, embedder)
    return corpus


@dataclass(frozen=True)
class Tables:
    """What the superuser sees: whose each document is, the sheets, and every trust."""

    owner: dict[str, str]
    sheets: frozenset[str]
    trust: dict[str, str]


@pytest.fixture(scope="module")
def tables(corpus: load.Corpus) -> Tables:
    with connect(SUPERUSER_URL) as su:
        documents = su.execute(
            sql.SQL("SELECT id, advisor_id, trust FROM {}").format(table("documents"))
        ).fetchall()
        sheets = su.execute(
            sql.SQL("SELECT id, trust FROM {}").format(table("product_sheets"))
        ).fetchall()
    return Tables(
        owner={record: advisor for record, advisor, _ in documents},
        sheets=frozenset(record for record, _ in sheets),
        trust={record: trust for record, *_, trust in documents + sheets},
    )


@pytest.fixture
def read(app_pool: ConnectionPool, embedder: OnnxEmbedder, corpus: load.Corpus) -> Any:
    return handlers.read_handlers(app_pool, embedder)


def unwrap(item: Untrusted[Any]) -> Any:
    """The value inside wrap()'s delimiter, as JSON."""
    lines = wrap(item).split("\n")
    assert lines[0].startswith("<untrusted ") and lines[-1] == "</untrusted>", lines[0]
    return json.loads(html.unescape("\n".join(lines[1:-1])))


def requests(pool: ConnectionPool) -> int:
    return pool.get_stats().get("requests_num", 0)


# The boundary.


@pytest.mark.parametrize("person", branding.ADVISORS, ids=lambda person: person.id)
@pytest.mark.parametrize("query", list(EVERY_QUERY.values()), ids=list(EVERY_QUERY))
def test_an_advisor_finds_their_own_records_only(
    person: branding.Person, query: str, read: Any, tables: Tables
) -> None:
    found = read[SEARCH](issue(person.id), SearchClientDocs(query=query, limit=20))
    ids = found.provenance.record_ids
    foreign = [
        record
        for record in ids
        if record not in tables.sheets and tables.owner.get(record) != person.id
    ]
    assert not foreign, f"{person.name} got records that are not theirs: {foreign}"
    assert any(tables.owner.get(record) == person.id for record in ids), ids
    hits = unwrap(found)
    assert [hit["document_id"] for hit in hits] == list(ids)
    assert found.provenance.tool == SEARCH


@pytest.mark.parametrize("query", list(FAMILY_QUERIES.values()), ids=list(FAMILY_QUERIES))
def test_the_demo_family_is_found_by_its_own_advisor(query: str, read: Any) -> None:
    # The positive side of the boundary test's other advisors: the family is findable.
    hits = unwrap(read[SEARCH](issue(DEMO.advisor_id), SearchClientDocs(query=query)))
    assert any(hit["mandate_id"] == DEMO.id for hit in hits), hits


# A foreign mandate.


def results(read: Any, person: branding.Person, mandate: str) -> tuple[Any, Any]:
    searched = read[SEARCH](issue(person.id), SearchClientDocs(query=FAMILY, mandate_id=mandate))
    report = read[PORTFOLIO](issue(person.id), GetPortfolio(mandate_id=mandate))
    return searched, report


def test_another_advisors_mandate_is_no_mandate(read: Any, corpus: load.Corpus) -> None:
    mine = next(person for person in branding.ADVISORS if person.id == DEMO.advisor_id)
    foreign = next(m.id for m in corpus.mandates if m.advisor_id != DEMO.advisor_id)
    made_up = f"{SEED}-no-such-mandate"
    answers = []
    for mandate in (foreign, made_up):
        searched, report = results(read, mine, mandate)
        assert unwrap(searched) == [] and searched.provenance.record_ids == ()
        assert unwrap(report) is None and report.provenance.record_ids == ()
        answers.append((wrap(searched), wrap(report)))
    assert answers[0] == answers[1], "a foreign mandate answers unlike a made-up one"


@pytest.mark.parametrize("person", OTHERS, ids=lambda person: person.id)
def test_the_demo_mandate_is_no_mandate_for_the_others(person: branding.Person, read: Any) -> None:
    searched, report = results(read, person, DEMO.id)
    assert unwrap(searched) == [] and unwrap(report) is None


def test_the_advisors_own_mandate(read: Any, corpus: load.Corpus) -> None:
    mine = issue(DEMO.advisor_id)
    documents = [d for d in corpus.documents if d.mandate_id == DEMO.id]
    assert 0 < len(documents) <= handlers.CANDIDATES, len(documents)
    searched = read[SEARCH](
        mine, SearchClientDocs(query=f"{FAMILY} Vermögensbericht", mandate_id=DEMO.id, limit=20)
    )
    hits = unwrap(searched)
    # One passage per corpus document (PR 4), so the vector branch reaches every one.
    assert sorted(hit["document_id"] for hit in hits) == sorted(d.id for d in documents)
    assert {(hit["kind"], hit["mandate_id"]) for hit in hits} == {(handlers.DOCUMENT, DEMO.id)}
    (expected,) = [d for d in documents if d.origin == "portfolio_system"]
    report = read[PORTFOLIO](mine, GetPortfolio(mandate_id=DEMO.id))
    assert report.provenance.record_ids == (expected.id,)
    assert unwrap(report) == {
        "mandate_id": DEMO.id,
        "document_id": expected.id,
        "title": expected.title,
        "lang": expected.lang,
        "report": expected.body,
    }


def test_an_email_dressed_as_the_report_is_not_the_portfolio(
    read: Any, corpus: load.Corpus, superuser: Connection
) -> None:
    (expected,) = [
        d for d in corpus.documents if d.mandate_id == DEMO.id and d.origin == "portfolio_system"
    ]
    planted = f"{SEED}-{DEMO.advisor_id}-dressed-report"
    # Control: by id alone the planted e-mail would come first.
    assert planted < expected.id
    canary = "cnry" + secrets.token_hex(6)
    row = {
        "advisor_id": DEMO.advisor_id,
        "id": planted,
        "mandate_id": DEMO.id,
        "title": expected.title,
        "body": f"{expected.body}\n{canary}",
        "lang": expected.lang,
        "origin": "mailbox",
    }
    columns = sql.SQL(", ").join(map(sql.Identifier, row))
    values = sql.SQL(", ").join(map(sql.Placeholder, row))
    superuser.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(table("documents"), columns, values), row
    )
    try:
        report = read[PORTFOLIO](issue(DEMO.advisor_id), GetPortfolio(mandate_id=DEMO.id))
    finally:
        superuser.execute(
            sql.SQL("DELETE FROM {} WHERE id = %s").format(table("documents")), (planted,)
        )
    assert report.provenance.record_ids == (expected.id,)
    assert canary not in wrap(report)


# Product sheets and trust.


@pytest.mark.parametrize("lang", ["english", "german"])
@pytest.mark.parametrize("person", branding.ADVISORS, ids=lambda person: person.id)
def test_product_sheets_reach_every_advisor(
    person: branding.Person, lang: str, read: Any, corpus: load.Corpus
) -> None:
    sheet = next(sheet for sheet in corpus.sheets if sheet.lang == lang)
    hits = unwrap(read[SEARCH](issue(person.id), SearchClientDocs(query=sheet.title)))
    found = [hit for hit in hits if hit["document_id"] == sheet.id]
    assert found, f"{person.name} did not find the {lang} sheet: {[h['document_id'] for h in hits]}"
    assert found[0] == {
        "kind": handlers.PRODUCT_SHEET,
        "document_id": sheet.id,
        "mandate_id": None,
        "title": sheet.title,
        "lang": lang,
        "trust": "internal",
        "excerpt": sheet.body,
    }


def test_trust_is_read_from_the_tables(read: Any, tables: Tables) -> None:
    seen = set()
    for person in branding.ADVISORS:
        for query in QUERIES.values():
            for hit in unwrap(
                read[SEARCH](issue(person.id), SearchClientDocs(query=query, limit=20))
            ):
                assert hit["trust"] == tables.trust[hit["document_id"]], hit["document_id"]
                seen.add(hit["trust"])
    # Non-vacuity: e-mails (mailbox) and the bank's own records both came back.
    assert seen == {"internal", "untrusted"}, seen


def test_every_document_hit_carries_its_closest_passage(
    read: Any, corpus: load.Corpus, embedder: OnnxEmbedder
) -> None:
    # One passage per corpus document, so the excerpt is that passage: never cut, and
    # the stored chunk text (the loader's chunk() of title and body), not the title.
    with connect(SUPERUSER_URL) as su:
        chunks = dict(
            su.execute(
                sql.SQL(
                    "SELECT document_id, body FROM {} WHERE advisor_id = %s AND embedding_model = %s"
                ).format(table("chunks")),
                (DEMO.advisor_id, embedder.model_id),
            ).fetchall()
        )
    hits = unwrap(
        read[SEARCH](issue(DEMO.advisor_id), SearchClientDocs(query=QUERIES["de-report"]))
    )
    documents = [hit for hit in hits if hit["kind"] == handlers.DOCUMENT]
    assert documents
    for hit in documents:
        assert hit["excerpt"] == chunks[hit["document_id"]], hit["document_id"]


# The iterative scan.

CROWD_MODEL = "fake/iterative-scan"
CROWD_REVISION = "probe"
CROWD_NEAR = 500  # the other advisor's chunks, nearest the query
CROWD_FAR = 30  # the advisor's own, further away
PENALTIES = (
    "SET LOCAL enable_seqscan = off",
    "SET LOCAL enable_bitmapscan = off",
    "SET LOCAL enable_sort = off",
)


def unit(weights: dict[int, float], jitter: int) -> list[float]:
    """A unit vector with the given components, nudged per chunk so none repeats."""
    values = [0.0] * DIM
    for index, weight in weights.items():
        values[index] = weight
    values[2 + jitter % (DIM - 2)] += 0.01
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def crowd_document(person: branding.Person) -> str:
    return f"{SEED}-{person.id}-crowd-d"


@pytest.fixture
def crowded(corpus: load.Corpus, superuser: Connection) -> Iterator[None]:
    """The other advisor's chunks crowd the query vector; the advisor's own sit further
    off. They carry a model id of their own, so nothing else is a candidate."""
    plan = ((OTHER, {0: 1.0}, CROWD_NEAR), (MINE, {0: 0.5, 1: 0.866}, CROWD_FAR))
    try:
        for person, weights, count in plan:
            mandate = next(m.id for m in corpus.mandates if m.advisor_id == person.id)
            superuser.execute(
                sql.SQL(
                    "INSERT INTO {} (advisor_id, id, mandate_id, title, body, lang, origin)"
                    " VALUES (%s, %s, %s, 'Crowd', 'crowd', 'english', 'call_notes')"
                ).format(table("documents")),
                (person.id, crowd_document(person), mandate),
            )
            with superuser.cursor() as cursor:
                cursor.executemany(
                    sql.SQL(
                        "INSERT INTO {} (advisor_id, id, document_id, ordinal, body, embedding,"
                        " embedding_model, embedding_revision)"
                        " VALUES (%s, %s, %s, %s, 'crowd', %s, %s, %s)"
                    ).format(table("chunks")),
                    [
                        (
                            person.id,
                            f"{crowd_document(person)}-{n}",
                            crowd_document(person),
                            n,
                            handlers.vector_text(unit(weights, n)),
                            CROWD_MODEL,
                            CROWD_REVISION,
                        )
                        for n in range(count)
                    ],
                )
        yield
    finally:
        for person, _, _ in plan:
            superuser.execute(
                sql.SQL("DELETE FROM {} WHERE document_id = %s").format(table("chunks")),
                (crowd_document(person),),
            )
            superuser.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(table("documents")),
                (crowd_document(person),),
            )


@pytest.mark.usefixtures("crowded")
def test_the_iterative_scan_fills_the_vector_branch(app_pool: ConnectionPool) -> None:
    query = handlers.vector_text(unit({0: 1.0}, 0))
    args = (query, CROWD_MODEL, CROWD_REVISION)
    with advisor_transaction(app_pool, issue(MINE.id)) as conn:
        setting = conn.execute("SELECT current_setting('hnsw.iterative_scan')").fetchone()[0]
        for statement in PENALTIES:
            conn.execute(statement)
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                "EXPLAIN (COSTS OFF) " + queries.NEAREST_CHUNKS, args
            ).fetchall()
        )
        found = handlers.nearest_chunks(conn, MINE.id, query, CROWD_MODEL, CROWD_REVISION)
        # Control: the same plan without the iterative scan.
        conn.execute("SET LOCAL hnsw.iterative_scan = off")
        control = handlers.nearest_chunks(conn, MINE.id, query, CROWD_MODEL, CROWD_REVISION)
    assert setting == session.ITERATIVE_SCAN
    assert "Index Scan using chunks_embedding" in plan, plan
    # Sorted again in full: PostgreSQL 17 would otherwise trust the relaxed order.
    assert plan.startswith("Sort") and "Presorted Key" not in plan, plan
    assert len(found) == handlers.CANDIDATES, found
    assert {document for document, _ in found} == {crowd_document(MINE)}
    assert [distance for _, distance in found] == sorted(distance for _, distance in found)
    assert len(control) < handlers.CANDIDATES, control


# Clean connections.


def test_the_handlers_leave_the_connection_clean(app_pool: ConnectionPool, read: Any) -> None:
    for person in branding.ADVISORS:
        read[SEARCH](issue(person.id), SearchClientDocs(query=QUERIES["de-report"]))
        read[PORTFOLIO](issue(person.id), GetPortfolio(mandate_id=DEMO.id))
    with app_pool.connection() as conn:
        found = session.problems(conn)
        state = conn.execute(
            "SELECT current_setting('app.advisor_id', true),"
            " current_setting('hnsw.iterative_scan', true)"
        ).fetchone()
    assert found == []
    assert state == ("", "off"), state
    # The guard closes a connection it refuses, and the pool opens another: one
    # connection in all means the guard passed it at every checkout.
    assert app_pool.get_stats().get("connections_num", 0) == 1, app_pool.get_stats()


# The index.

LEAKPROOF = (
    "SELECT p.proname, p.proleakproof FROM pg_operator o JOIN pg_proc p ON p.oid = o.oprcode"
    " WHERE (o.oprname = '@@' AND o.oprleft = 'tsvector'::regtype"
    " AND o.oprright = 'tsquery'::regtype)"
    " OR (o.oprname = '<=>' AND o.oprleft = 'vector'::regtype)"
)
TEXT_INDEXES = (
    "SELECT i.indexrelid::regclass::text FROM pg_index i"
    " JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " JOIN pg_class x ON x.oid = i.indexrelid JOIN pg_am a ON a.oid = x.relam"
    " WHERE n.nspname = %s AND c.relname IN ('documents', 'product_sheets')"
    " AND a.amname IN ('gin', 'gist')"
)


def test_no_index_on_tsv_can_serve_the_app_role(superuser: Connection) -> None:
    assert dict(superuser.execute(LEAKPROOF).fetchall()) == {
        "ts_match_vq": False,
        "cosine_distance": False,
    }
    # ADR 0013: none is built, since none could be used.
    assert superuser.execute(TEXT_INDEXES, (names.SCHEMA,)).fetchall() == []
    explain = "EXPLAIN (COSTS OFF) " + queries.SEARCH_DOCUMENTS_ENGLISH
    with superuser.transaction(force_rollback=True):
        superuser.execute(
            sql.SQL("CREATE INDEX probe_tsv ON {} USING gin (tsv)").format(table("documents"))
        )
        superuser.execute("SET LOCAL enable_seqscan = off")
        superuser.execute(
            sql.SQL("SET LOCAL search_path = {}, public").format(sql.Identifier(names.SCHEMA))
        )
        bypassing = [row[0] for row in superuser.execute(explain, ("portfolio",)).fetchall()]
        superuser.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(names.APP_ROLE)))
        superuser.execute("SELECT set_config('app.advisor_id', %s, true)", (MINE.id,))
        scoped = [row[0] for row in superuser.execute(explain, ("portfolio",)).fetchall()]
    assert any("probe_tsv" in line for line in bypassing), bypassing
    assert not any("probe_tsv" in line for line in scoped), scoped
    assert any("documents_pkey" in line for line in scoped), scoped


# Refusals and logs.


def test_a_query_over_the_models_limit_is_refused(
    app_pool: ConnectionPool, read: Any, embedder: OnnxEmbedder
) -> None:
    # A ligature that NFKC spells out in many letters: within the schema's 500
    # characters, over the model's 512 tokens (measured: 130 of them make about 525).
    query = "ﷺ" * 130
    assert len(query) <= 500, len(query)
    assert embedder.tokenizer.count(query_input(query)) > MAX_TOKENS
    before = requests(app_pool)
    with pytest.raises(handlers.ToolFailed) as refused:
        read[SEARCH](issue(MINE.id), SearchClientDocs(query=query))
    assert refused.value.kind == handlers.QUERY_TOO_LONG == str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert requests(app_pool) == before, "a connection was taken for a refused query"


def test_no_query_text_reaches_a_log_record(read: Any, caplog: pytest.LogCaptureFixture) -> None:
    canary = "cnry" + secrets.token_hex(6)
    # psycopg holds its own logger at WARNING; here the driver and the pool log all.
    with (
        caplog.at_level(logging.DEBUG),
        caplog.at_level(logging.DEBUG, logger="psycopg"),
        caplog.at_level(logging.DEBUG, logger="psycopg.pool"),
    ):
        # Control: the capture sees the driver's DEBUG records.
        logging.getLogger("psycopg").debug("control %s", "record")
        read[SEARCH](issue(MINE.id), SearchClientDocs(query=f"{canary} Vermögensbericht"))
        read[PORTFOLIO](issue(MINE.id), GetPortfolio(mandate_id=canary))
    assert [r.getMessage() for r in caplog.records if r.name == "psycopg"][:1] == ["control record"]
    # Measured: about twenty records of the driver and the pool for these two calls.
    assert sum(r.name == "psycopg.pool" for r in caplog.records) > 1, "the pool logged nothing"
    logged = "\n".join(
        f"{record.getMessage()} {record.args!r} {record.exc_text}" for record in caplog.records
    )
    assert canary not in logged
