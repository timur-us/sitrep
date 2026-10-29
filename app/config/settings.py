"""Runtime settings from the environment. Scaffolding: the rules arrive in the next commit."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", frozen=True)

    host: str = "127.0.0.1"
    port: int = 8000
    dev_auth: str = "0"
    trace_plaintext: str = "0"

    @property
    def dev_auth_on(self) -> bool:
        return False

    @property
    def trace_plaintext_on(self) -> bool:
        return False
