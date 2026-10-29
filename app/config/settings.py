"""Runtime settings, from the environment only.

No implicit dotenv: `Settings()` reads environment variables and nothing else. Only the
entry point, `python -m app`, passes `_env_file=".env"`; tests and offline runners build
`Settings(_env_file=None)`.

Flags are typed `str` and are on only for exactly "1": "true", "yes", " 1" and "01"
are off, so a typo fails closed.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK = "127.0.0.1"


def flag(value: str) -> bool:
    return value == "1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, frozen=True, extra="ignore")

    host: str = LOOPBACK
    port: int = Field(default=8000, ge=0, le=65535)
    # The dev login shim (PR 7). Off in the image.
    dev_auth: str = "0"
    # Spans to the console without the allowlist, for local debugging only: refused
    # unless the exporter is the console and the entry point binds 127.0.0.1 (ADR 0007).
    trace_plaintext: str = "0"

    @property
    def dev_auth_on(self) -> bool:
        return flag(self.dev_auth)

    @property
    def trace_plaintext_on(self) -> bool:
        return flag(self.trace_plaintext)
