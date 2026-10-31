"""NAIVE (PR 5b, commit 1): a plain pool and an advisor transaction without guards.

Commit 3 replaces this file before merge; the tests of commit 2 go red on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool


class GuardError(Exception):
    pass


def make_pool(url: str, *, max_size: int = 4, timeout: float = 5.0) -> ConnectionPool:
    return ConnectionPool(
        url,
        kwargs={"autocommit": True},
        min_size=1,
        max_size=max_size,
        open=False,
        timeout=timeout,
    )


def problems(conn: psycopg.Connection[Any]) -> list[str]:
    return []


@contextmanager
def advisor_transaction(pool: ConnectionPool, advisor: Any) -> Iterator[psycopg.Connection[Any]]:
    advisor_id = getattr(advisor, "advisor_id", advisor)
    with pool.connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL app.advisor_id = '{advisor_id}'")
        yield conn
