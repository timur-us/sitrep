"""app.db.connect: the URL check holds at connect time, libpq's environment cannot steer
the connection, and no keyword argument can replace a connection parameter.
"""

from __future__ import annotations

import psycopg
import pytest

from app.config.settings import DATABASE_PORT, Settings
from app.db import names
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")

# Another loopback address: nothing is published there, so a connection to it fails
# without leaving the machine.
ELSEWHERE = "127.0.0.2"
# libpq variables that would move the connection, change who logs in, or change the
# session's settings (sec-1, sec-3 of the PR 5a review).
STEERING = {
    "PGHOSTADDR": ELSEWHERE,
    "PGHOST": ELSEWHERE,
    "PGPORT": "5432",
    "PGOPTIONS": "-c search_path=pg_catalog",
    "PGUSER": "postgres",
    "PGDATABASE": "postgres",
    "PGSERVICE": "elsewhere",
    "PGTZ": "Pacific/Chatham",
    "PGDATESTYLE": "German",
    "PGAPPNAME": "steered",
}


def test_controls_libpq_reads_the_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without app.db.connect, these variables take effect: the refusal below is not vacuous.
    url = settings.database_url
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=pg_catalog")
    monkeypatch.setenv("PGTZ", "Pacific/Chatham")
    with psycopg.connect(url) as plain:
        assert plain.execute("SHOW search_path").fetchone() == ("pg_catalog",)
        assert plain.execute("SHOW timezone").fetchone() == ("Pacific/Chatham",)
    monkeypatch.delenv("PGOPTIONS")
    monkeypatch.delenv("PGTZ")
    # A URL without a port goes wherever PGPORT says.
    portless = f"host=127.0.0.1 user={names.APP_ROLE} dbname={names.DATABASE}"
    monkeypatch.setenv("PGPORT", str(DATABASE_PORT))
    with psycopg.connect(portless) as plain:
        assert plain.execute("SELECT current_user").fetchone() == (names.APP_ROLE,)
    monkeypatch.delenv("PGPORT")
    # PGHOSTADDR wins over the host: a URL naming an address where nothing listens connects.
    unreachable = url.replace("127.0.0.1", ELSEWHERE)
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.1")
    with psycopg.connect(unreachable) as plain:
        assert plain.execute("SELECT current_user").fetchone() == (names.APP_ROLE,)


@pytest.mark.parametrize(("name", "value"), sorted(STEERING.items()))
def test_connect_refuses_libpq_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"libpq environment variables are set: \\['{name}'\\]"):
        connect(settings.database_url)


def test_connect_names_the_variables_not_their_values(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPASSWORD", "pw0rdCanary")
    with pytest.raises(ValueError) as error:
        connect(settings.database_url)
    assert "PGPASSWORD" in str(error.value)
    assert "pw0rdCanary" not in str(error.value)


@pytest.mark.parametrize(
    "keyword",
    ["host", "hostaddr", "port", "user", "dbname", "options", "service", "password", "sslmode"],
)
def test_connect_takes_no_connection_parameter(settings: Settings, keyword: str) -> None:
    # psycopg merges keyword arguments over the URL: connect(url, user="postgres") would
    # log in as the superuser past the URL check.
    with pytest.raises(TypeError, match=keyword):
        connect(settings.database_url, **{keyword: "postgres"})


def test_connect_takes_connection_options(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True, prepare_threshold=None) as conn:
        assert conn.autocommit
        assert conn.execute("SELECT current_user").fetchone() == (names.APP_ROLE,)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://x@{ELSEWHERE}:1/db",
        "host=127.0.0.1 port=1 dbname=d",
        f"postgresql://{names.APP_ROLE}@127.0.0.1/{names.DATABASE}",
    ],
)
def test_connect_checks_the_url(url: str) -> None:
    # Refused before libpq is called. A connect that ran would fail too, but with
    # OperationalError; the hosts stay on loopback either way.
    with pytest.raises(ValueError, match="host must be|missing"):
        connect(url)
