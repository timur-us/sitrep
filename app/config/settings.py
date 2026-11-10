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

The model API key (M1 PR 6, ADR 0012) is a SecretStr read from ANTHROPIC_API_KEY, the one
name without the prefix; only the model client's factory reads its value. While
PYDANTIC_SETTINGS_DEBUG is set, in any case and to any value, Settings refuses to build
before any source is read: pydantic-settings would log every raw value, the key's too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.config import branding
from app.db import names

LOOPBACK = "127.0.0.1"
# Every setting is read from a variable with the slug as prefix (ADR 0009), so a
# shell's own HOST (tcsh exports one) cannot move the bind. The database URLs too, since
# M1 PR 3 (Timur's answer 1 to the PR 7 questions): a URL exported for another tool
# under the bare name is ignored, never refused, and never connected to.
ENV_PREFIX = f"{branding.SLUG.upper()}_"


# The model API key keeps its own name (the M1 plan's default 7): the secret hook and the
# GitHub checklist key on it. It is the one setting without the prefix.
KEY_FIELD = "anthropic_api_key"
KEY_VARIABLE = "ANTHROPIC_API_KEY"
# The runtime model (ADR 0003), read from the prefixed variable (env_name("model"));
# app/harness/model_client.py refuses one without a price.
DEFAULT_MODEL = "claude-sonnet-5-5"
# What a key may look like: printable ASCII, no space. Anything else could not be sent as
# a header, and the HTTP library's error would quote it.
KEY_SHAPE = re.compile(r"[\x21-\x7e]+")
# pydantic-settings logs every source's raw values at DEBUG while this is set to a truthy
# value, a dotenv key included (review finding sec-3 of M1 PR 3).
SETTINGS_DEBUG = "PYDANTIC_SETTINGS_DEBUG"


def env_name(field: str) -> str:
    """The environment variable a settings field is read from."""
    return KEY_VARIABLE if field == KEY_FIELD else ENV_PREFIX + field.upper()


class SettingsRefused(ValueError):
    """An environment Settings refuses to build in. The message names, never values."""


def settings_debug_variables(environ: Mapping[str, str]) -> list[str]:
    """The names, in any case, under which the settings debug switch is set."""
    return sorted(name for name in environ if name.upper() == SETTINGS_DEBUG)


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
    # The model (ADR 0012), from the prefixed variable like every other setting.
    model: str = Field(default=DEFAULT_MODEL, min_length=1)
    # The model API key, from ANTHROPIC_API_KEY (no prefix). Empty is no key. A SecretStr:
    # repr, str and JSON show stars; only the model client's factory reads the value.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias=KEY_VARIABLE)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Asked before any source is read, and so before pydantic-settings' debug log of
        # their values: with the switch set at all, whatever its value, nothing is read.
        found = settings_debug_variables(os.environ)
        if found:
            raise SettingsRefused(
                f"{', '.join(found)} is set: pydantic-settings would log every setting's"
                f" value, {KEY_VARIABLE} included"
            )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator(KEY_FIELD, mode="before")
    @classmethod
    def _empty_is_no_key(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(KEY_FIELD)
    @classmethod
    def _key_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not KEY_SHAPE.fullmatch(value.get_secret_value()):
            raise ValueError("not a key: printable ASCII without spaces only")
        return value

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
