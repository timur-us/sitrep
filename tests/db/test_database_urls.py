"""DATABASE_URL and MIGRATION_DATABASE_URL: one host, exactly 127.0.0.1, and nothing
beyond user, host, port and database. Read by libpq's own parser, so a URL is judged as
the connection would read it. No database needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import DATABASE_PORT, Settings, loopback_database_url
from app.db import names

FIELDS = ("DATABASE_URL", "MIGRATION_DATABASE_URL")
BASE = f"postgresql://{names.APP_ROLE}@127.0.0.1:{DATABASE_PORT}/{names.DATABASE}"
CANARY = "pw0rdCanary"  # a planted password, never a real one
# Built at runtime, so the secret scan does not see a password in a URL here.
WITH_PASSWORD = BASE.replace("@", ":" + CANARY + "@", 1)

REJECTED = [
    (BASE.replace("127.0.0.1", "192.0.2.1"), "host must be"),
    (BASE.replace("127.0.0.1", "localhost"), "host must be"),
    (BASE.replace("127.0.0.1", "127.0.0.2"), "host must be"),
    (BASE.replace("127.0.0.1", "[::1]"), "host must be"),
    (BASE.replace("127.0.0.1", "0.0.0.0"), "host must be"),  # noqa: S104 - planted
    (BASE.replace("127.0.0.1:", "127.0.0.1,192.0.2.1:"), "host must be"),
    (BASE.replace(f"127.0.0.1:{DATABASE_PORT}", "127.0.0.1:1,127.0.0.1:2"), "host must be|port"),
    (BASE.replace(f":{DATABASE_PORT}", ":0"), "port"),
    (BASE + "?options=-c%20search_path%3Dpublic", "['options']"),
    (BASE + "?hostaddr=192.0.2.1", "['hostaddr']"),
    (BASE + "?service=x", "['service']"),
    (BASE + "?sslmode=disable", "['sslmode']"),
    (BASE + "?host=192.0.2.1", "host must be"),
    (f"postgresql://{names.APP_ROLE}@/{names.DATABASE}?host=/tmp", "host must be"),
    (f"postgresql://127.0.0.1:{DATABASE_PORT}/{names.DATABASE}", "missing ['user']"),
    (f"postgresql://{names.APP_ROLE}@127.0.0.1:{DATABASE_PORT}", "missing ['dbname']"),
    (f"postgresql://{names.APP_ROLE}@:{DATABASE_PORT}/{names.DATABASE}", "missing ['host']"),
    (WITH_PASSWORD, "['password']"),
    (f"host=127.0.0.1 hostaddr=192.0.2.1 user=u dbname={names.DATABASE}", "['hostaddr']"),
    ("not a url = =", "not a libpq connection string"),
]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(("url", "rule"), REJECTED)
def test_database_urls_are_loopback(
    monkeypatch: pytest.MonkeyPatch, field: str, url: str, rule: str
) -> None:
    monkeypatch.setenv(field, url)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    message = str(error.value)
    # A rule is a substring; "a|b" accepts either.
    assert any(part in message for part in rule.split("|")), message
    assert field.lower() in message, message
    # The rejected value is not echoed: it may carry a password.
    assert CANARY not in message
    assert url not in message


@pytest.mark.parametrize(
    "url",
    [
        BASE,
        f"postgresql://{names.MIGRATOR_ROLE}@127.0.0.1/{names.DATABASE}",
        f"host=127.0.0.1 port={DATABASE_PORT} user={names.APP_ROLE} dbname={names.DATABASE}",
    ],
)
def test_loopback_urls_are_accepted(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    for field in FIELDS:
        monkeypatch.setenv(field, url)
    settings = Settings(_env_file=None)
    assert (settings.database_url, settings.migration_database_url) == (url, url)


def test_defaults_are_the_two_login_roles(settings: Settings) -> None:
    assert settings.database_url == loopback_database_url(names.APP_ROLE)
    assert settings.migration_database_url == loopback_database_url(names.MIGRATOR_ROLE)
    assert settings.database_url == BASE
