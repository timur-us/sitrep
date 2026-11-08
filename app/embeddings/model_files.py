"""Scaffolding (M1 PR 4, the tests-first commit): the names the tests import, nothing more.

The model files module replaces this file in the next commit.
"""

from __future__ import annotations

__all__ = (
    "HUB",
    "MODELS",
    "ModelFileError",
    "missing",
    "model_dir",
    "read_verified",
    "url",
    "verify",
)

HUB = ""
MODELS = None


class ModelFileError(Exception):
    pass


def model_dir(*args: object) -> None:
    raise NotImplementedError("M1 PR 4")


def url(rel: str) -> str:
    raise NotImplementedError("M1 PR 4")


def read_verified(*args: object) -> bytes:
    raise NotImplementedError("M1 PR 4")


def missing(*args: object) -> list[tuple[str, object]]:
    raise NotImplementedError("M1 PR 4")


def verify(*args: object) -> list[str]:
    raise NotImplementedError("M1 PR 4")
