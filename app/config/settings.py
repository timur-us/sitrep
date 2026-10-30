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

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db import names

LOOPBACK = "127.0.0.1"
# compose.yaml publishes the database here: not 5432, where a native PostgreSQL may listen.
DATABASE_PORT = 55432

# The libpq parameters a database URL may set.
URL_KEYS = frozenset({"user", "host", "port", "dbname"})
URL_REQUIRED = URL_KEYS
# The role each URL must log in as.
URL_ROLES = {"database_url": names.APP_ROLE, "migration_database_url": names.MIGRATOR_ROLE}


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
        env_file=None, frozen=True, extra="ignore", hide_input_in_errors=True
    )

    # The app connects as the app role; the migration runner as the migrator.
    database_url: str = loopback_database_url(names.APP_ROLE)
    migration_database_url: str = loopback_database_url(names.MIGRATOR_ROLE)

    host: str = LOOPBACK
    port: int = Field(default=8000, ge=0, le=65535)
    # The dev login shim (PR 7). Off in the image.
    dev_auth: str = "0"
    # Spans to the console without the allowlist, for local debugging only: refused
    # unless the exporter is the console and the entry point binds 127.0.0.1 (ADR 0007).
    trace_plaintext: str = "0"

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

    @property
    def trace_plaintext_on(self) -> bool:
        return flag(self.trace_plaintext)
