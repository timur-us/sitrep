"""The model client (ADR 0012): scaffolding for the tests of M1 PR 6, inert until the
next commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config.settings import Settings

BASE_URL = "https://api.anthropic.com"
SDK_PREFIX = "ANTHROPIC_"
MAX_RETRIES = 0
TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 5.0
PRICES_AS_OF = "2026-11-09"


@dataclass(frozen=True)
class Price:
    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal


PRICES: Mapping[str, Price] = {}
GEO_MULTIPLIERS: Mapping[str, Decimal] = {}


class ModelClientRefused(ValueError):
    pass


class ModelCallError(Exception):
    def __init__(self, kind: str, status_code: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


class PriceUnknown(ValueError):
    pass


def refused_variables(environ: Mapping[str, str]) -> list[str]:
    return []


def check_start(settings: Settings, environ: Mapping[str, str]) -> None:
    return None


class AnthropicModelClient:
    def create(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError


def build_model_client(settings: Settings, *, transport: object = None) -> AnthropicModelClient:
    raise NotImplementedError


def cost_usd(model: str, usage: Mapping[str, Any]) -> Decimal:
    raise NotImplementedError
