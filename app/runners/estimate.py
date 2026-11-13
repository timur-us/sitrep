"""The cost estimate before a live run: `python -P -m app.runners.evals --estimate`.

Offline: no key, no network, no database. The cost is asks × tokens per ask × the price
table of app/harness/model_client.py, at the US-only rate (1.1 times the table), the
higher of the two geographies it knows:

- **tokens per ask** are measured where they can be: per case, the mean usage of its
  recorded samples at the current prompt version (the route's body reports it). A case
  without one takes ASSUMED, the tokens of a three-to-four-turn ask on this corpus: the
  prompt and tools are about 1,000 tokens, each search result about 1,300 to 1,600 and
  the portfolio about 250 (counted with the loop's own estimate on the loaded corpus,
  M1 PR 16), and an answer of about a page;
- **the ceiling** is every ask spending its whole token budget (app/harness/loop.py,
  REQUEST_BUDGET), as much of it output as MAX_TURNS replies of MAX_TOKENS can be. The
  budget may overshoot by one call's estimate error, so the ceiling is not a hard bound.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from app.config.settings import DEFAULT_MODEL
from app.harness import loop
from app.harness.model_client import PRICES_AS_OF, cost_usd
from app.harness.prompt import PROMPT_VERSION

ASSUMED = {"input_tokens": 12_000, "output_tokens": 1_500}
GEO = "us"
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Estimate:
    asks: int
    measured_cases: int
    tokens: Mapping[str, int]  # per ask, over all asks (rounded down)
    expected_usd: Decimal
    ceiling_usd: Decimal


def _cost(input_tokens: int, output_tokens: int) -> Decimal:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "inference_geo": GEO}
    return cost_usd(DEFAULT_MODEL, usage)


def ceiling_per_ask() -> Decimal:
    output = min(loop.MAX_TURNS * loop.MAX_TOKENS, loop.REQUEST_BUDGET)
    return _cost(loop.REQUEST_BUDGET - output, output)


def measured(path: Path) -> dict[str, int] | None:
    """The mean usage of a recording's answered samples, or None without one."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        recording = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    counts = []
    for sample in recording.get("samples", []) if isinstance(recording, dict) else []:
        body = sample.get("response", {}).get("body") if isinstance(sample, dict) else None
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict) and all(
            type(usage.get(key)) is int and usage[key] >= 0 for key in ASSUMED
        ):
            counts.append(usage)
    if not counts:
        return None
    return {key: sum(usage[key] for usage in counts) // len(counts) for key in ASSUMED}


def estimate(directory: Path, case_ids: Sequence[str], samples: int) -> Estimate:
    per_case: list[Mapping[str, Any]] = []
    found = 0
    for case_id in case_ids:
        usage = measured(directory / "recorded" / PROMPT_VERSION / f"{case_id}.json")
        found += usage is not None
        per_case.append(usage or ASSUMED)
    asks = len(case_ids) * samples
    expected = sum(
        (_cost(u["input_tokens"], u["output_tokens"]) * samples for u in per_case), Decimal(0)
    )
    tokens = {
        key: (sum(u[key] for u in per_case) // len(per_case) if per_case else 0) for key in ASSUMED
    }
    return Estimate(
        asks=asks,
        measured_cases=found,
        tokens=tokens,
        expected_usd=expected.quantize(CENT, rounding=ROUND_CEILING),
        ceiling_usd=(ceiling_per_ask() * asks).quantize(CENT, rounding=ROUND_CEILING),
    )


def lines(result: Estimate, cases: int, samples: int) -> list[str]:
    source = (
        f"measured for {result.measured_cases} of {cases} cases, assumed for the rest"
        if result.measured_cases
        else f"assumed: no {PROMPT_VERSION} recording yet"
    )
    head = (
        f"evals: estimate, prompt {PROMPT_VERSION}, {DEFAULT_MODEL}:"
        f" {cases} cases x {samples} samples = {result.asks} asks"
    )
    tokens = (
        f"evals: tokens per ask: {result.tokens['input_tokens']} input,"
        f" {result.tokens['output_tokens']} output ({source})"
    )
    expected = (
        f"evals: expected cost: USD {result.expected_usd}"
        f" (the price table of {PRICES_AS_OF}, at the US-only rate)"
    )
    ceiling = (
        f"evals: ceiling: USD {result.ceiling_usd} (every ask spends its whole budget of"
        f" {loop.REQUEST_BUDGET} tokens)"
    )
    return [head, tokens, expected, ceiling]
