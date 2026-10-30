"""The one way to open a database connection (ADR 0008).

NAIVE, fail-first commit of M0 PR 5a: the URL is not checked and libpq's environment
is not pinned. The next commit adds both.
"""

from __future__ import annotations

from typing import Any

import psycopg

CONNECT_TIMEOUT_SECONDS = 5


def connect(url: str, **kwargs: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS, **kwargs)
