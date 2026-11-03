"""The tool registry. Scaffolding: it registers anything and validates nothing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

IDENTITY_TERMS: tuple[str, ...] = ()


def identity_term(name: str) -> str | None:
    return None


class Effect(StrEnum):
    READ = "read"
    DRAFT = "draft"
    SIDE_EFFECT = "side_effect"


class DataClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    result: Any
    effect: Effect
    data_class: DataClass
    requires_approval: bool


class RegistrationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class CallError:
    type: str
    loc: tuple[str | int, ...]
    msg: str


@dataclass(frozen=True)
class ValidCall:
    spec: ToolSpec
    arguments: BaseModel


@dataclass(frozen=True)
class RejectedCall:
    tool: str | None
    errors: tuple[CallError, ...]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def schema(self, name: str) -> dict[str, Any]:
        return self._specs[name].arguments.model_json_schema()

    def validate_call(self, name: str, arguments: object) -> ValidCall | RejectedCall:
        spec = self._specs.get(name)
        if spec is None:
            return ValidCall(spec, None)  # type: ignore[arg-type]
        fields = arguments if isinstance(arguments, dict) else {}
        return ValidCall(spec, spec.arguments.model_construct(**fields))
