"""Shared fixtures.

Every test gets settings built without a dotenv file (`Settings(_env_file=None)`),
from an environment that carries none of the settings' own variables, so the
developer's shell does not leak into a test.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest

from app.config.settings import Settings
from app.observability.logs import LOG_CONFIG


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    fields = {name.lower() for name in Settings.model_fields}
    for name in list(os.environ):
        # pydantic-settings matches variable names without regard to case.
        if name.lower() in fields:
            monkeypatch.delenv(name)
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
