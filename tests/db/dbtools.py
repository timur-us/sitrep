"""Shared by the database tests: the superuser URL and catalog helpers."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from app.config.settings import loopback_database_url
from app.db import names

SUPERUSER = "postgres"
SUPERUSER_URL = loopback_database_url(SUPERUSER)

Connection = psycopg.Connection[Any]
RECORD = sql.Identifier(names.META_SCHEMA, "schema_migrations")


def applied(superuser: Connection) -> list[tuple[Any, ...]]:
    """The runner's record, in version order."""
    query = sql.SQL(
        "SELECT version, file_name, sha256, session_user_name, current_user_name"
        " FROM {} ORDER BY version"
    ).format(RECORD)
    return superuser.execute(query).fetchall()


def column(conn: Connection, query: str | sql.Composable, params: Any = ()) -> list[Any]:
    return [row[0] for row in conn.execute(query, params).fetchall()]
