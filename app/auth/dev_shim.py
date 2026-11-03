"""The dev login shim (PR 7). Scaffolding: nothing is mounted yet."""

from __future__ import annotations

from fastapi import FastAPI

COOKIE = "dev_session"


def mount(app: FastAPI, port: int) -> None:
    """Scaffolding: mounts nothing."""
    return
