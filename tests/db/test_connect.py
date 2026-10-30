"""app.db.connect: the URL check holds at connect time, and libpq's environment cannot
send the connection elsewhere or add server settings at login.
"""

from __future__ import annotations

import psycopg
import pytest

from app.config.settings import Settings
from app.db import names
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")

# Another loopback address: nothing is published there, so a connection to it fails
# without leaving the machine.
ELSEWHERE = "127.0.0.2"


def test_environment_cannot_redirect_or_configure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = settings.database_url
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=pg_catalog")
    # Controls: without the pins, both variables take effect.
    with psycopg.connect(url) as plain:
        assert plain.execute("SHOW search_path").fetchone() == ("pg_catalog",)
    monkeypatch.setenv("PGHOSTADDR", ELSEWHERE)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(url).close()

    with connect(url) as pinned:
        search_path = pinned.execute("SHOW search_path").fetchone()
    assert search_path == (f"{names.SCHEMA}, public",), f"PGOPTIONS applied: {search_path}"


@pytest.mark.parametrize(
    "url", [f"postgresql://x@{ELSEWHERE}:1/db", "host=127.0.0.1 port=1 dbname=d"]
)
def test_connect_checks_the_url(url: str) -> None:
    # Refused before libpq is called. A connect that ran would fail too, but with
    # OperationalError; the hosts stay on loopback either way.
    with pytest.raises(ValueError, match="host must be|missing"):
        connect(url)
