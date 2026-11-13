"""The reviewed corpus in the database, for the tests that read it (M1 PRs 8 and 12).

Moved here from tests/db/test_retrieval.py unchanged, so tests/db/test_ask.py checks the
same rows: `make load`'s, or loaded by the first module that needs them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dbtools import SUPERUSER_URL
from psycopg import sql

from app.config.settings import loopback_database_url
from app.db import load, names
from app.db.connect import connect
from app.db.migrate import become_owner
from app.embeddings.onnx import OnnxEmbedder
from data.synthetic import generate

MIGRATOR_URL = loopback_database_url(names.MIGRATOR_ROLE)


def table(name: str) -> sql.Identifier:
    return sql.Identifier(names.SCHEMA, name)


def is_loaded(corpus: load.Corpus, embedder: OnnxEmbedder) -> bool:
    """Every corpus document and sheet is in the database, as the corpus has it (title and
    body too, so a load from an older generator is reloaded), with a vector of this model."""
    model = (embedder.model_id, embedder.revision)
    with connect(SUPERUSER_URL) as su:
        documents = set(
            su.execute(
                sql.SQL(
                    "SELECT d.advisor_id, d.id, d.title, d.body FROM {} d JOIN {} c"
                    " ON c.advisor_id = d.advisor_id AND c.document_id = d.id"
                    " WHERE c.embedding_model = %s AND c.embedding_revision = %s"
                ).format(table("documents"), table("chunks")),
                model,
            ).fetchall()
        )
        sheets = {
            tuple(row)
            for row in su.execute(
                sql.SQL(
                    "SELECT id, title, body FROM {}"
                    " WHERE embedding_model = %s AND embedding_revision = %s"
                ).format(table("product_sheets")),
                model,
            ).fetchall()
        }
    wanted = {(d.advisor_id, d.id, d.title, d.body) for d in corpus.documents}
    wanted_sheets = {(sheet.id, sheet.title, sheet.body) for sheet in corpus.sheets}
    return wanted <= documents and wanted_sheets <= sheets


def ensure_loaded(embedder: OnnxEmbedder, out: Path) -> load.Corpus:
    """The reviewed corpus, generated into out, and in the database: loaded if it is not."""
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
    """What the superuser sees: whose each document is, the sheets, every title, origin
    and trust."""

    owner: dict[str, str]
    sheets: frozenset[str]
    trust: dict[str, str]
    title: dict[str, str]
    origin: dict[str, str]


def read_tables() -> Tables:
    with connect(SUPERUSER_URL) as su:
        documents = su.execute(
            sql.SQL("SELECT id, advisor_id, origin, trust, title FROM {}").format(
                table("documents")
            )
        ).fetchall()
        sheets = su.execute(
            sql.SQL("SELECT id, origin, trust, title FROM {}").format(table("product_sheets"))
        ).fetchall()
    return Tables(
        owner={record: advisor for record, advisor, _, _, _ in documents},
        sheets=frozenset(record for record, _, _, _ in sheets),
        trust={row[0]: row[-2] for row in documents + sheets},
        title={row[0]: row[-1] for row in documents + sheets},
        origin={row[0]: row[-3] for row in documents + sheets},
    )
