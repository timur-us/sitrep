"""DATABASE_URL and MIGRATION_DATABASE_URL: one host, exactly 127.0.0.1, an explicit port,
and nothing beyond user, host, port and database. Each one names its own role (the app
role, the migrator) and the app's database. Read by libpq's own parser, so a URL is
judged as the connection would read it. No database needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import DATABASE_PORT, Settings, loopback_database_url
from app.db import names

ROLE_OF = {"DATABASE_URL": names.APP_ROLE, "MIGRATION_DATABASE_URL": names.MIGRATOR_ROLE}
CANARY = "pw0rdCanary"  # a planted password, never a real one
DB = names.DATABASE
PORT = DATABASE_PORT

# "{base}" is the field's own valid URL, "{role}" its role. Each case breaks one rule.
REJECTED = [
    (f"postgresql://{{role}}@192.0.2.1:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@localhost:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.0.0.2:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.1:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.000.000.001:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@[::1]:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@[::ffff:127.0.0.1]:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.0.0.1.:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@0.0.0.0:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.0.0.1,192.0.2.1:{PORT}/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.0.0.1:1,127.0.0.1:2/{DB}", "host must be"),
    (f"postgresql://{{role}}@127.0.0.1/{DB}", "missing ['port']"),
    (f"postgresql://{{role}}@127.0.0.1:0/{DB}", "port must be one number"),
    (f"postgresql://{{role}}@127.0.0.1:65536/{DB}", "port must be one number"),
    (f"host=127.0.0.1 port=+{PORT} user={{role}} dbname={DB}", "port must be one number"),
    (f"host=127.0.0.1 port=abc user={{role}} dbname={DB}", "port must be one number"),
    ("host=127.0.0.1 port=٥٥٤٣٢ user={role} dbname=" + DB, "port must be"),
    ("{base}?options=-c%20search_path%3Dpublic", "['options']"),
    ("{base}?hostaddr=192.0.2.1", "['hostaddr']"),
    ("{base}?%68ostaddr=192.0.2.1", "hostaddr|libpq"),
    ("{base}?service=x", "['service']"),
    ("{base}?sslmode=disable", "['sslmode']"),
    ("{base}?host=192.0.2.1", "host must be"),
    ("{base}?host=127.0.0.1&host=192.0.2.1", "host must be"),
    (f"postgresql://{{role}}@/{DB}?host=/tmp&port={PORT}", "host must be"),
    (f"postgresql://127.0.0.1:{PORT}/{DB}", "missing ['user']"),
    (f"host=127.0.0.1 port={PORT} user='' dbname={DB}", "missing ['user']"),
    (f"postgresql://{{role}}@127.0.0.1:{PORT}", "missing ['dbname']"),
    (f"postgresql://{{role}}@:{PORT}/{DB}", "missing ['host']"),
    ("postgresql://{role}:" + CANARY + f"@127.0.0.1:{PORT}/{DB}", "['password']"),
    (f"host=127.0.0.1 hostaddr=192.0.2.1 port={PORT} user={{role}} dbname={DB}", "['hostaddr']"),
    ("not a url = =", "not a libpq connection string"),
    # Loopback and well-formed, but another role or database than the field's own.
    (f"postgresql://postgres@127.0.0.1:{PORT}/{DB}", "must log in as"),
    (f"postgresql://{names.OWNER_ROLE}@127.0.0.1:{PORT}/{DB}", "must log in as"),
    (f"postgresql://{{role}}@127.0.0.1:{PORT}/postgres", f"database must be {DB}"),
]


def planted(template: str, field: str) -> str:
    role = ROLE_OF[field]
    return template.replace("{base}", loopback_database_url(role)).replace("{role}", role)


@pytest.mark.parametrize("field", sorted(ROLE_OF))
@pytest.mark.parametrize(("template", "rule"), REJECTED)
def test_database_urls_are_loopback(
    monkeypatch: pytest.MonkeyPatch, field: str, template: str, rule: str
) -> None:
    url = planted(template, field)
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


@pytest.mark.parametrize("field", sorted(ROLE_OF))
def test_each_url_takes_only_its_own_role(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    other = next(role for name, role in ROLE_OF.items() if name != field)
    monkeypatch.setenv(field, loopback_database_url(other))
    with pytest.raises(ValidationError, match="must log in as"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "template",
    [
        "{base}",
        f"host=127.0.0.1 port={PORT} user={{role}} dbname={DB}",
        f"postgresql://{{role}}@127.0.0.1:{PORT}/{DB}?",
    ],
)
def test_loopback_urls_are_accepted(monkeypatch: pytest.MonkeyPatch, template: str) -> None:
    urls = {field: planted(template, field) for field in ROLE_OF}
    for field, url in urls.items():
        monkeypatch.setenv(field, url)
    settings = Settings(_env_file=None)
    assert settings.database_url == urls["DATABASE_URL"]
    assert settings.migration_database_url == urls["MIGRATION_DATABASE_URL"]


def test_defaults_are_the_two_login_roles(settings: Settings) -> None:
    expected = f"postgresql://{names.APP_ROLE}@127.0.0.1:55432/{names.DATABASE}"
    assert settings.database_url == expected
    assert settings.migration_database_url == expected.replace(names.APP_ROLE, names.MIGRATOR_ROLE)
