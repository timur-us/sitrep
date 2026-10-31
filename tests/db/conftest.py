"""Database tests run against compose.yaml's PostgreSQL, migrated.

When it is not there they fail with what to run, and never skip: a skipped role test
would pass CI with the boundary untested. The superuser connection is for inspection
only; a test that makes a claim about a role connects as that role.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from dbtools import SUPERUSER_URL, Connection

from app.config.settings import DATABASE_PORT
from app.db.connect import connect
from app.db.migrate import RECORD


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
