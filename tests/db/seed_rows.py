"""Rows for the row-level security tests, and the catalog discovery they are driven by.

The superuser inserts the rows; it bypasses row-level security, so it also sees every
row, which makes it the reference count. Each table the app role can read needs a
factory in FACTORIES: the tests discover the readable tables from pg_catalog and fail
on a table without one, so a new table cannot arrive without its fail-closed test.

Every id starts with SEED and names its advisor, so the seed never collides with other
data and the cleanup removes exactly what it inserted. Advisor ids come from branding.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from dbtools import SUPERUSER_URL, Connection
from psycopg import sql

from app.config import branding
from app.config.embedding import DIM
from app.db import names
from app.db.connect import connect

SEED = "rls-seed"
# The advisors whose rows the tests compare: the demo mandate's advisor, and another.
MINE = next(person for person in branding.ADVISORS if person.id == branding.DEMO_MANDATE.advisor_id)
OTHER = next(person for person in branding.ADVISORS if person is not MINE)

# Relations in a schema that is not a system schema, readable by the connected role.
READABLE = (
    "SELECT n.nspname, c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,"
    " coalesce(c.reloptions, '{}')"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'"
    " AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
    " AND has_any_column_privilege(c.oid, 'SELECT')"
    " ORDER BY 1, 2"
)


def readable(conn: Connection) -> list[tuple[Any, ...]]:
    return conn.execute(READABLE).fetchall()


def discovered_tables(conn: Connection) -> list[str]:
    """The readable relations as schema.name, as the connected role sees them."""
    return [f"{schema}.{name}" for schema, name, *_ in readable(conn)]


def vector(seed: int) -> str:
    """A DIM-dimensional vector literal."""
    values = [0.0] * DIM
    values[seed % DIM] = 1.0
    return "[" + ",".join(str(value) for value in values) + "]"


def rows_for(advisor_id: str, suffix: str, parent: str = "1") -> dict[str, dict[str, Any]]:
    """One row per table for advisor_id, parents first; children point at the parent
    rows with suffix `parent`, which the seed creates."""
    key = f"{SEED}-{advisor_id}"
    return {
        "advisors": {"advisor_id": advisor_id, "display_name": f"Seed advisor {advisor_id}"},
        "mandates": {"advisor_id": advisor_id, "id": f"{key}-{suffix}-m", "family": "Seedfamily"},
        "documents": {
            "advisor_id": advisor_id,
            "id": f"{key}-{suffix}-d",
            "mandate_id": f"{key}-{parent}-m",
            "title": "Seed report",
            "body": "Quarterly portfolio report, seeded for the boundary tests.",
            "lang": "english",
        },
        "chunks": {
            "advisor_id": advisor_id,
            "id": f"{key}-{suffix}-c",
            "document_id": f"{key}-{parent}-d",
            "ordinal": 0,
            "body": "Quarterly portfolio report.",
            "embedding": vector(0),
            "embedding_model": "fake/seed",
            "embedding_revision": "seed",
        },
    }


# The tables a factory exists for, in insert order.
FACTORIES = tuple(rows_for("x", "1"))
TABLES = frozenset(f"{names.SCHEMA}.{table}" for table in FACTORIES)


def table_id(table: str) -> sql.Identifier:
    return sql.Identifier(names.SCHEMA, table.rpartition(".")[2])


def insert_statement(table: str, row: dict[str, Any], *, if_absent: bool = False) -> sql.Composed:
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        table_id(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in row),
        sql.SQL(", ").join(sql.Placeholder(column) for column in row),
    )
    if if_absent:
        statement += sql.SQL(" ON CONFLICT DO NOTHING")
    return statement


def insert(conn: Connection, table: str, row: dict[str, Any], *, if_absent: bool = False) -> int:
    return conn.execute(insert_statement(table, row, if_absent=if_absent), row).rowcount


def key_of(table: str, row: dict[str, Any]) -> tuple[str, Any]:
    """The key column besides advisor_id, and its value."""
    column = "advisor_id" if table.rpartition(".")[2] == "advisors" else "id"
    return column, row[column]


def cleanup(conn: Connection, advisors: list[str]) -> None:
    """Delete every seeded row, children first, and the advisors the seed inserted."""
    for table in reversed(FACTORIES):
        if table == "advisors":
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE advisor_id = ANY(%s)").format(table_id(table)),
                (advisors,),
            )
        else:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id LIKE %s").format(table_id(table)),
                (SEED + "-%",),
            )


@contextmanager
def seeded_database() -> Iterator[None]:
    """One row per table for each advisor, committed; removed again afterwards."""
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        cleanup(conn, [])
        inserted: list[str] = []
        with conn.transaction():
            for person in branding.ADVISORS:
                for table, row in rows_for(person.id, "1").items():
                    count = insert(conn, f"{names.SCHEMA}.{table}", row, if_absent=True)
                    if table == "advisors" and count:
                        inserted.append(person.id)
        try:
            yield
        finally:
            cleanup(conn, inserted)


def count(conn: Connection, table: str, advisor_id: str | None = None) -> int:
    query = sql.SQL("SELECT count(*) FROM {}").format(table_id(table))
    if advisor_id is None:
        return conn.execute(query).fetchone()[0]
    query += sql.SQL(" WHERE advisor_id = %s")
    return conn.execute(query, (advisor_id,)).fetchone()[0]
