"""Runtime settings, from the environment only.

No implicit dotenv: `Settings()` reads environment variables and nothing else. Only the
entry point, `python -m app`, passes `_env_file=".env"`; tests and offline runners build
`Settings(_env_file=None)`.

Flags are typed `str` and are on only for exactly "1": "true", "yes", " 1" and "01"
are off, so a typo fails closed.

Database URLs name one host, exactly 127.0.0.1, an explicit port, a user and a database,
and nothing else: no password, no hostaddr, no options, no service (ADR 0008). A missing
parameter would come from libpq's environment, so all four are required. Each field
names its own role and the app's database. URLs are parsed by libpq itself, so the
check reads them as the connection will. A rejected value is not echoed in the error.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import branding
from app.db import names

LOOPBACK = "127.0.0.1"
# Every setting is read from a variable with the slug as prefix (ADR 0009), so a
# shell's own HOST (tcsh exports one) cannot move the bind. The database URLs too, since
# M1 PR 3 (Timur's answer 1 to the PR 7 questions): a URL exported for another tool
# under the bare name is ignored, never refused, and never connected to.
ENV_PREFIX = f"{branding.SLUG.upper()}_"


def env_name(field: str) -> str:
    """The environment variable a settings field is read from."""
    return ENV_PREFIX + field.upper()


# The fields renamed in M1 PR 3. Their old names may still be exported for this project,
# so the entry points say they are ignored. HOST and the other M0 names are not
# reported: shells export HOST for themselves.
RENAMED = ("database_url", "migration_database_url")


def ignored_url_notices(environ: Mapping[str, str]) -> list[str]:
    """One notice per unprefixed database URL variable set in environ, in any case
    (review finding sec-1): the name as set, and the names that are read. Never a value.
    """
    read = " and ".join(env_name(field) for field in RENAMED)
    return [
        f"ignored: {name} is set, but the database URLs are read from {read} only (ADR 0009)"
        for name in sorted(environ)
        if name.lower() in RENAMED
    ]


# compose.yaml publishes the database here: not 5432, where a native PostgreSQL may listen.
DATABASE_PORT = 55432

# The libpq parameters a database URL may set.
URL_KEYS = frozenset({"user", "host", "port", "dbname"})
URL_REQUIRED = URL_KEYS
# The role each URL must log in as.
URL_ROLES = {"database_url": names.APP_ROLE, "migration_database_url": names.MIGRATOR_ROLE}


# One Host the app answers (M1 PR 5): a lower-case DNS name or a dotted IPv4 address,
# optionally with a port of 1 to 65535 without a leading zero. The Host header must equal
# one of them byte for byte; no wildcard, scheme, path, user or IPv6 literal.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_ITEM = re.compile(rf"{_LABEL}(?:\.{_LABEL})*(?::[1-9][0-9]{{0,4}})?")


def check_allowed_hosts(value: str) -> str:
    """The allowlist unchanged, or ValueError naming the rule it breaks (never a value).
    Empty means the default, 127.0.0.1 on the app's port."""
    if value == "":
        return value
    for item in value.split(","):
        if not HOST_ITEM.fullmatch(item):
            raise ValueError(
                "a comma-separated list of lower-case host[:port], without spaces,"
                " wildcards, schemes or paths"
            )
        name, _, port = item.partition(":")
        if port and int(port) > 65535:
            raise ValueError("a port must be from 1 to 65535")
        # A name that anyone on the network can answer reopens DNS rebinding (review
        # finding sec-5 of M1 PR 5): a single label (search domains, LLMNR) or .local (mDNS).
        if (name != "localhost" and "." not in name) or name.endswith(".local"):
            raise ValueError(
                "a single-label or .local name is resolved locally: name a full DNS name"
            )
    return value


def flag(value: str) -> bool:
    return value == "1"


def loopback_database_url(role: str) -> str:
    return f"postgresql://{role}@{LOOPBACK}:{DATABASE_PORT}/{names.DATABASE}"


def check_database_url(url: str) -> str:
    """The URL unchanged, or ValueError naming the rule it breaks (never its values)."""
    try:
        params = conninfo_to_dict(url)
    except ProgrammingError:
        raise ValueError("not a libpq connection string") from None
    extra = sorted(set(params) - URL_KEYS)
    if extra:
        raise ValueError(f"parameters beyond user, host, port and dbname: {extra}")
    missing = sorted(URL_REQUIRED - {key for key, value in params.items() if value})
    if missing:
        raise ValueError(f"missing {missing}: libpq would take them from the environment")
    if params["host"] != LOOPBACK:
        raise ValueError(f"host must be exactly {LOOPBACK}: one host, no name, no socket")
    port = params["port"]
    if not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        raise ValueError("port must be one number from 1 to 65535")
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix=ENV_PREFIX,
        frozen=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # The app connects as the app role; the migration runner as the migrator. The alias
    # is the prefixed name (env_name()), so a refusal names the variable, not the field.
    database_url: str = Field(
        default=loopback_database_url(names.APP_ROLE), validation_alias=env_name("database_url")
    )
    migration_database_url: str = Field(
        default=loopback_database_url(names.MIGRATOR_ROLE),
        validation_alias=env_name("migration_database_url"),
    )

    host: str = LOOPBACK
    port: int = Field(default=8000, ge=0, le=65535)
    # The dev login shim (ADR 0009): on only for exactly "1", and only when the entry
    # point binds exactly 127.0.0.1. It is not in the image.
    dev_auth: str = "0"
    # Spans to the console without the allowlist, for local debugging only: refused
    # unless the exporter is the console and the entry point binds 127.0.0.1 (ADR 0007).
    trace_plaintext: str = "0"
    # The Hosts the app answers, on every route, with or without the dev login (M1 PR 5,
    # ADR 0009): empty for exactly 127.0.0.1:<port>; the image's smoke test names its
    # published address, M3 the Azure host. Anything else is 400.
    allowed_hosts: str = Field(default="", validation_alias=env_name("allowed_hosts"))

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _loopback_only(cls, value: str, info: ValidationInfo) -> str:
        params = conninfo_to_dict(check_database_url(value))
        role = URL_ROLES[info.field_name]
        if params["user"] != role:
            raise ValueError(f"must log in as {role}")
        if params["dbname"] != names.DATABASE:
            raise ValueError(f"database must be {names.DATABASE}")
        return value

    @property
    def dev_auth_on(self) -> bool:
        return flag(self.dev_auth)

    @field_validator("allowed_hosts")
    @classmethod
    def _allowed_hosts(cls, value: str) -> str:
        return check_allowed_hosts(value)

    @property
    def allowed_host_values(self) -> tuple[str, ...]:
        """The Host values the app answers, the default filled in."""
        if self.allowed_hosts == "":
            return (f"{LOOPBACK}:{self.port}",)
        return tuple(self.allowed_hosts.split(","))

    @property
    def trace_plaintext_on(self) -> bool:
        return flag(self.trace_plaintext)
