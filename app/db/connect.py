"""The one way to open a database connection (ADR 0008).

The URL is checked again here: one host, exactly 127.0.0.1, an explicit port, a user and
a database, nothing else. libpq fills every parameter a connection string leaves out
from PG* environment variables, and several of them steer a connection the URL check
has passed: PGHOSTADDR moves it to another address whatever the host says, PGOPTIONS,
PGTZ and PGDATESTYLE set server settings at login, PGSERVICE pulls in a service file.
The checks therefore refuse to connect while any PG* variable is set, naming the
variables, never their values. As a second line, hostaddr and options are passed
explicitly, which libpq lets win over the environment.

Keyword arguments are connection options only: psycopg merges keywords over the URL, so
connect(url, user=...) would log in as someone the check never saw.

The checks live in CheckedConnection.connect(). psycopg_pool does not call connect():
it opens connections with connection_class.connect(conninfo, **kwargs), so
app/db/session.py gives its pool this class, and pooled connections pass the same checks.
"""

from __future__ import annotations

import os
from typing import Any, Self

import psycopg

from app.config.settings import LOOPBACK, check_database_url

CONNECT_TIMEOUT_SECONDS = 5
# psycopg.Connection.connect options that are not libpq connection parameters.
CONNECTION_OPTIONS = frozenset({"autocommit", "prepare_threshold"})


def libpq_environment() -> list[str]:
    """The names of the PG* variables set in this process."""
    return sorted(name for name in os.environ if name.startswith("PG"))


class CheckedConnection(psycopg.Connection[Any]):
    """A psycopg connection that can only be opened through the checks above."""

    @classmethod
    def connect(cls, conninfo: str = "", **options: Any) -> Self:
        check_database_url(conninfo)
        unknown = sorted(set(options) - CONNECTION_OPTIONS)
        if unknown:
            raise TypeError(f"connect() takes no connection parameter: {unknown}")
        variables = libpq_environment()
        if variables:
            raise ValueError(f"libpq environment variables are set: {variables}; unset them")
        return super().connect(
            conninfo,
            hostaddr=LOOPBACK,
            options="",
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            **options,
        )


def connect(url: str, **options: Any) -> CheckedConnection:
    return CheckedConnection.connect(url, **options)
