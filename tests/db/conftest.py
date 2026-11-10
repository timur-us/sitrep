"""Database tests run against compose.yaml's PostgreSQL, migrated.

When it is not there they fail with what to run, and never skip: a skipped role test
would pass CI with the boundary untested. The superuser connection is for inspection
only; a test that makes a claim about a role connects as that role.

The READ handlers embed every query (M1 PR 8), so the tests that call them need the
real model as well (`make model`); without it the `embedder` fixture fails, never skips,
as tests/embeddings does.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from dbtools import SUPERUSER_URL, Connection
from forged_payloads import BODIES, token_document_id
from psycopg import sql
from psycopg_pool import ConnectionPool
from seed_rows import MINE, OTHER, insert, rows_for, seeded_database

from app.config.settings import DATABASE_PORT, Settings
from app.db import names, session
from app.db.connect import connect
from app.db.migrate import RECORD
from app.embeddings.model_files import ModelFileError
from app.embeddings.onnx import OnnxEmbedder


@pytest.fixture(scope="session")
def embedder() -> OnnxEmbedder:
    """The real embedding model, loaded once per session (tests/db/test_loader.py keeps
    its own, per module)."""
    try:
        return OnnxEmbedder()
    except ModelFileError as error:
        pytest.fail(f"the embedding model is not in place ({error}); run `make model`")


@pytest.fixture(scope="session")
def database() -> None:
    try:
        with connect(SUPERUSER_URL) as conn:
            migrated = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (RECORD,)).fetchone()
    except psycopg.OperationalError as error:
        pytest.fail(
            f"PostgreSQL does not answer on 127.0.0.1:{DATABASE_PORT} "
            f"({type(error).__name__}): run `make up`, then `make migrate`",
            pytrace=False,
        )
    if migrated != (True,):
        pytest.fail(f"{RECORD} does not exist: run `make migrate`", pytrace=False)


@pytest.fixture
def superuser(database: None) -> Iterator[Connection]:
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="module")
def seeded(database: None) -> Iterator[None]:
    """One row per mandate-scoped table for each advisor, and one product sheet, per test
    module: tests/db/test_loader.py replaces every advisor's rows, the seed's included,
    so a module seeds for itself and leaves the corpus as it found it."""
    with seeded_database():
        yield


@pytest.fixture
def app_pool(settings: Settings, database: None) -> Iterator[ConnectionPool]:
    """The app's pool on settings' database_url, one connection, opened for one test."""
    with session.make_pool(settings.database_url, max_size=1, timeout=5.0) as pool:
        yield pool


@pytest.fixture(scope="module")
def token_documents(seeded: None) -> Iterator[None]:
    """forged_payloads.WORD in one document per advisor (MINE, OTHER) and language."""
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        planted = []
        for person in (MINE, OTHER):
            for lang, body in BODIES.items():
                row = {
                    "advisor_id": person.id,
                    "id": token_document_id(person.id, lang),
                    "mandate_id": rows_for(person.id, "1")["mandates"]["id"],
                    "title": "Token note",
                    "body": body,
                    "lang": lang,
                    "origin": "call_notes",
                }
                insert(conn, f"{names.SCHEMA}.documents", row)
                planted.append(row["id"])
        try:
            yield
        finally:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(
                    sql.Identifier(names.SCHEMA, "documents")
                ),
                (planted,),
            )
