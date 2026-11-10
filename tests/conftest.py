"""Shared fixtures.

Every test gets settings built without a dotenv file (`Settings(_env_file=None)`),
from an environment that carries none of the settings' own variables, so the
developer's shell does not leak into a test.

The same fixture removes the proxy variables and sets NO_PROXY to "*": pytest-socket
allows loopback, so a proxy on 127.0.0.1 would turn an allowed connect into egress,
for the test and for any subprocess it starts. It also removes every OTEL_* variable:
the SDK and the instrumentation read them (disable tracing, exclude URLs, capture
headers), and a test that needs one sets it itself. And it removes every PG* variable:
libpq reads them for whatever a connection string leaves out, and app.db.connect refuses
to connect while one is set (ADR 0008).

Since M1 PR 6 it removes every variable the model SDK reads (the ANTHROPIC_ prefix, in
any case, the key included: the key is a settings field) and PYDANTIC_SETTINGS_DEBUG,
under which Settings refuses to build (review finding sec-3 of M1 PR 3).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import warnings
from collections.abc import Iterator

import pytest

from app.config.settings import SETTINGS_DEBUG, Settings, env_name
from app.harness.model_client import SDK_PREFIX
from app.observability.logs import LOG_CONFIG

PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy"})


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # The variables the settings read (prefixed, ADR 0009), and the bare field names,
    # which they read before the prefix.
    fields = {name.lower() for name in Settings.model_fields}
    fields |= {env_name(name).lower() for name in Settings.model_fields}
    for name in list(os.environ):
        # pydantic-settings, like most proxy clients, matches names without regard to case.
        if (
            name.lower() in fields | PROXY_VARIABLES
            or name.upper().startswith(("OTEL_", SDK_PREFIX))
            or name.upper() == SETTINGS_DEBUG
            or name.startswith("PG")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    return Settings(_env_file=None)


@pytest.fixture(autouse=True)
def restore_exception_hooks() -> Iterator[None]:
    """Put back the process-wide hooks app.__main__.main() replaces (M1 PR 5), so a test
    that runs main() in process does not leave the app's hooks for the next one, and
    pytest's own thread and unraisable hooks stay in place (review finding test-1)."""
    saved = (threading.excepthook, sys.unraisablehook, sys.excepthook, warnings.showwarning)
    yield
    threading.excepthook, sys.unraisablehook, sys.excepthook, warnings.showwarning = saved


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
