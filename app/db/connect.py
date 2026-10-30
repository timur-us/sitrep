"""The one way to open a database connection (ADR 0008).

The URL is checked again here: one host, exactly 127.0.0.1, nothing beyond user, host,
port and database. libpq fills every parameter a connection string leaves out from PG*
environment variables, and two of them would undo that check: PGHOSTADDR sends the
connection to another address whatever the host says, and PGOPTIONS sets server
settings at login. Both are therefore passed explicitly, which libpq lets win over the
environment. The other PG* variables can still change TLS or the service file lookup,
not the address or the session's settings.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.config.settings import LOOPBACK, check_database_url

CONNECT_TIMEOUT_SECONDS = 5


def connect(url: str, **kwargs: Any) -> psycopg.Connection[Any]:
    check_database_url(url)
    return psycopg.connect(
        url,
        hostaddr=LOOPBACK,
        options="",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        **kwargs,
    )
