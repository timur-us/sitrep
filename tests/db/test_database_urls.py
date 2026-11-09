"""The two database URLs, read from their prefixed variables (ADR 0009; M1 PR 3): one
host, exactly 127.0.0.1, an explicit port, and nothing beyond user, host, port and
database. Each one names its own role (the app role, the migrator) and the app's
database. Read by libpq's own parser, so a URL is judged as the connection would read
it. The unprefixed names are ignored, in the environment and in a dotenv file. No
database needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import branding
from app.config.settings import DATABASE_PORT, Settings, env_name, loopback_database_url
from app.db import names

FIELDS = {"database_url": names.APP_ROLE, "migration_database_url": names.MIGRATOR_ROLE}
# The variable each URL is read from -> its role.
ROLE_OF = {env_name(field): role for field, role in FIELDS.items()}
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
    # The message names the variable, not the field (the alias in app/config/settings.py).
    assert field in message, message
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
    urls = {field: planted(template, env_name(field)) for field in FIELDS}
    for field, url in urls.items():
        monkeypatch.setenv(env_name(field), url)
    settings = Settings(_env_file=None)
    assert settings.database_url == urls["database_url"]
    assert settings.migration_database_url == urls["migration_database_url"]


def test_defaults_are_the_two_login_roles(settings: Settings) -> None:
    expected = f"postgresql://{names.APP_ROLE}@127.0.0.1:55432/{names.DATABASE}"
    assert settings.database_url == expected
    assert settings.migration_database_url == expected.replace(names.APP_ROLE, names.MIGRATOR_ROLE)


# M1 PR 3 (Timur's answer 1 to the PR 7 questions): the prefix, from branding rather than
# from env_name() itself, and the old names, which no longer do anything.
PREFIX = f"{branding.SLUG.upper()}_"
# Valid for its field and not the default: read, it shows as a value.
OTHER_PORT = "host=127.0.0.1 port=55433 user={role} dbname=" + DB
# Refused by the host rule: read, it raises.
FOREIGN_HOST = "postgresql://{role}@192.0.2.1:" + f"{PORT}/{DB}"


@pytest.mark.parametrize("spelling", [str.upper, str.lower])
def test_prefixed_names_are_read(monkeypatch: pytest.MonkeyPatch, spelling) -> None:
    for field, role in FIELDS.items():
        monkeypatch.setenv(spelling(PREFIX + field.upper()), OTHER_PORT.format(role=role))
    settings = Settings(_env_file=None)
    assert settings.database_url == OTHER_PORT.format(role=names.APP_ROLE)
    assert settings.migration_database_url == OTHER_PORT.format(role=names.MIGRATOR_ROLE)


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize("spelling", [str.upper, str.lower, str.title])
def test_unprefixed_names_change_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, field: str, spelling
) -> None:
    # No error, and the default stands: the variable is not read at all.
    monkeypatch.setenv(spelling(field.upper()), FOREIGN_HOST.format(role=FIELDS[field]))
    fresh = Settings(_env_file=None)
    assert fresh == settings
    assert getattr(fresh, field) == loopback_database_url(FIELDS[field])
    # Control: the same value under the prefixed name is refused, so it would matter.
    monkeypatch.setenv(PREFIX + field.upper(), FOREIGN_HOST.format(role=FIELDS[field]))
    with pytest.raises(ValidationError, match="host must be"):
        Settings(_env_file=None)


def test_unprefixed_names_in_a_dotenv_change_nothing(settings: Settings, tmp_path: Path) -> None:
    # A dotenv file from before M1 PR 3 still has the old lines, and `python -m app`
    # reads one: it must neither fail on them nor take them.
    lines = {field: FOREIGN_HOST.format(role=role) for field, role in FIELDS.items()}
    old = tmp_path / "old.env"
    old.write_text("".join(f"{field.upper()}={url}\n" for field, url in lines.items()))
    assert Settings(_env_file=old) == settings
    # Control: the same lines under the prefixed names are refused.
    new = tmp_path / "new.env"
    new.write_text("".join(f"{PREFIX}{field.upper()}={url}\n" for field, url in lines.items()))
    with pytest.raises(ValidationError, match="host must be"):
        Settings(_env_file=new)
