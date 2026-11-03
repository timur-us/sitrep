"""The context layer. Scaffolding: Untrusted renders like the value it holds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Provenance:
    tool: str = ""
    record_ids: tuple[str, ...] = ()


class Untrusted[T]:
    def __init__(self, value: T, provenance: Provenance) -> None:
        self.value = value
        self.provenance = provenance
        self.ref = "0"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)

    def __add__(self, other: str) -> str:
        return str(self.value) + other

    def __radd__(self, other: str) -> str:
        return other + str(self.value)

    def __repr__(self) -> str:
        return f"Untrusted({self.value!r}, {self.provenance!r})"


def wrap(item: Untrusted[Any]) -> str:
    return str(item)


class BudgetExceeded(Exception):
    pass


class TokenBudget(Protocol):
    def remaining(self) -> int: ...

    def charge(self, tokens: int) -> None: ...
