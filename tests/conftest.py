"""Shared fixtures.

Every test gets settings built without a dotenv file (`Settings(_env_file=None)`),
from an environment that carries none of the settings' own variables, so the
developer's shell does not leak into a test.

The same fixture removes the proxy variables and sets NO_PROXY to "*": pytest-socket
allows loopback, so a proxy on 127.0.0.1 would turn an allowed connect into egress,
for the test and for any subprocess it starts. It also removes every OTEL_* variable:
the SDK and the instrumentation read them (disable tracing, exclude URLs, capture
headers), and a test that needs one sets it itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest

from app.config.settings import Settings
from app.observability.logs import LOG_CONFIG

PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy"})


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    fields = {name.lower() for name in Settings.model_fields}
    for name in list(os.environ):
        # pydantic-settings, like most proxy clients, matches names without regard to case.
        if name.lower() in fields | PROXY_VARIABLES or name.upper().startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    return Settings(_env_file=None)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo the dictConfig that uvicorn.Config applies, so later tests see pytest's logging."""
    names = [None, "uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"]
    names += [name for name in LOG_CONFIG.get("loggers", {}) if name not in names]
    saved = []
    for name in names:
        logger = logging.getLogger(name)
        state = (logger.level, logger.handlers[:], logger.filters[:], logger.propagate)
        saved.append((logger, state, logger.disabled))
    yield
    for logger, (level, handlers, filters, propagate), disabled in saved:
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.propagate = propagate
        logger.disabled = disabled
