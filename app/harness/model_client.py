"""The model client (ADR 0003, ADR 0012): one factory, built from settings.

build_model_client() is the only way to a client. It refuses, naming variables and
never their values:
- any variable whose name starts with ANTHROPIC_, in any case, but the key itself. The
  SDK reads such variables for the base URL, other credentials (a token, profiles, a
  config directory, workload identity), extra headers, its logging and its OpenTelemetry
  content mode (anthropic 2.3.0, read while building; tests/harness/test_model_client.py
  re-reads the installed source). A prefix, not a list, so a variable the next SDK adds
  is refused too;
- a missing key, and a model the dated price table does not know.

The client it builds:
- gets the key explicitly, so the SDK reads no credential from the environment or from
  profiles on disk, and the base URL as a constant;
- sends through an httpx2 client built here, with trust_env off: no proxy variable, no
  SSL_CERT_* and no netrc. The SDK's own DefaultHttpxClient mounts proxies from the
  environment whatever trust_env says, so it is not used (and is banned in app/);
- follows no redirect, retries nothing (retries are the loop's, ADR 0001), and times out
  after TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS to connect;
- has the SDK's spans and trace-context propagation off: the loop traces with approved
  attributes only (ADR 0007), and no trace id leaves with a request;
- raises ModelCallError with a kind and an HTTP status, nothing else: the SDK's exception
  carries the response body and the request, so it is dropped, not chained.

At DEBUG the SDK logs each request's options, the prompt included, and each response's
headers, which a hostile server could fill with the key it was sent; httpcore2 logs the
response headers too. The factory holds the three libraries' loggers (SDK_LOGGERS) at
INFO or above, a child set lower included, so a root logger at DEBUG does not reach
them. ANTHROPIC_LOG would lower them: `import anthropic` applies it at import, before
any refusal, and the factory and the entry point then refuse it. Code that lowers a
logger after the build is not stopped.

create() sends tools of exactly the registry's shape, {name, description, input_schema}
(RequestRefused otherwise, before any request): a server tool or an extra field would
let the API fetch a URL or change the request on the model's behalf (ADR 0012). The
messages are not checked here: a document or image block with a URL source would make
the API fetch it too. The loop builds them and must pass thinking blocks back, so that
check is PR 10's (handed over in the PR 6 description).

cost_usd() is usage times PRICES, from Anthropic's pricing page on PRICES_AS_OF.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import anthropic
import httpx2

from app.config.settings import KEY_VARIABLE, Settings, env_name

BASE_URL = "https://api.anthropic.com"
# Every SDK variable starts with it; only the key is read (default 7 of the M1 plan).
SDK_PREFIX = "ANTHROPIC_"
# The model's name before M1 PR 6, named in the refusal.
OLD_MODEL_VARIABLE = "ANTHROPIC_MODEL"
MAX_RETRIES = 0
# One non-streaming answer must arrive within this (M1 has no streaming, default 10).
TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 5.0
MODEL_VARIABLE = env_name("model")
# The SDK's loggers and its HTTP library's: never below INFO (the docstring says why).
SDK_LOGGERS = ("anthropic", "httpx2", "httpcore2")
SDK_LOGGERS_MIN_LEVEL = logging.INFO
# The one shape a tool may have: the registry's (app/harness/tools.py, ToolRegistry).
TOOL_KEYS = frozenset({"name", "description", "input_schema"})

# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
# as read on PRICES_AS_OF. Global routing; a change is a dated edit, here and in ADR 0012.
PRICES_AS_OF = "2026-11-09"


@dataclass(frozen=True)
class Price:
    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal


PRICES: Mapping[str, Price] = MappingProxyType(
    {
        "claude-sonnet-5-5": Price(
            input=Decimal(2),
            cache_write_5m=Decimal("2.50"),
            cache_write_1h=Decimal(4),
            cache_read=Decimal("0.10"),
            output=Decimal(10),
        ),
    }
)
# The page's data residency pricing: US-only inference costs 1.1 times every category.
GEO_MULTIPLIERS: Mapping[str, Decimal] = MappingProxyType(
    {"global": Decimal(1), "us": Decimal("1.1")}
)
PER_TOKEN = Decimal(1_000_000)


class ModelClientRefused(ValueError):
    """A configuration the factory or the entry point refuses. Names, never values."""


class ModelCallError(Exception):
    """A model call that failed: "timeout", "connection", "status" (with the HTTP status)
    or "response" (a body the SDK could not read). Carries nothing from the request or
    the response, so it is safe in a log line or a span."""

    def __init__(self, kind: str, status_code: int | None = None) -> None:
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"the model call failed: {kind}{status}")
        self.kind = kind
        self.status_code = status_code


class RequestRefused(ValueError):
    """A request the client does not send: a tool not of the registry's shape. A
    programming error in the caller; the message names the rule, never a value."""


class PriceUnknown(ValueError):
    """Usage the table cannot price: another model, another geography, or token counts
    that are not counts."""


def refused_variables(environ: Mapping[str, str]) -> list[str]:
    """The names, sorted, of every SDK variable set in environ but the key, in any case."""
    return sorted(
        name
        for name in environ
        if name.upper().startswith(SDK_PREFIX) and name.upper() != KEY_VARIABLE
    )


def check_start(settings: Settings, environ: Mapping[str, str]) -> None:
    """Raise ModelClientRefused for an SDK variable in environ or a model without a price.
    The entry point calls it before it binds; the factory calls it again."""
    refused = refused_variables(environ)
    if refused:
        hint = (
            f"; {OLD_MODEL_VARIABLE} is now {MODEL_VARIABLE}"
            if OLD_MODEL_VARIABLE in {name.upper() for name in refused}
            else ""
        )
        raise ModelClientRefused(
            f"set, and refused: {', '.join(refused)}. The model client reads {KEY_VARIABLE}"
            f" and {MODEL_VARIABLE} only (ADR 0012){hint}"
        )
    if settings.model not in PRICES:
        raise ModelClientRefused(
            f"{MODEL_VARIABLE} names a model without a price in the table of {PRICES_AS_OF}"
        )


class AnthropicModelClient:
    """The ModelClient of app/harness/model.py, on the direct Messages API."""

    def __init__(self, sdk: anthropic.Anthropic, model: str) -> None:
        self._sdk = sdk
        self.model = model

    def __repr__(self) -> str:
        return f"AnthropicModelClient(model={self.model!r})"

    def create(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> Mapping[str, Any]:
        check_tools(tools)
        failure: ModelCallError | None = None
        try:
            message = self._sdk.messages.create(
                model=self.model,
                system=system,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                max_tokens=max_tokens,
            )
            if not _is_message(message):
                # A 200 that is not JSON comes back as its text (review finding sec-2), and
                # valid JSON is built leniently into a partial message (test-r2).
                failure = ModelCallError("response")
        except anthropic.APIError as error:
            failure = _call_error(error)
        except Exception:  # noqa: BLE001 - a body the SDK cannot parse, e.g. invalid JSON
            failure = ModelCallError("response")
        # Raised outside the except block, so the SDK's exception is neither the cause nor
        # the context: it holds the request (the key's header) and the response body.
        if failure is not None:
            raise failure
        return message.to_dict(mode="json")


def _is_message(message: object) -> bool:
    """A message the loop can read: a content list and a usage mapping, at least."""
    if not isinstance(message, anthropic.types.Message):
        return False
    fields = message.to_dict(mode="json")
    return isinstance(fields.get("content"), list) and isinstance(fields.get("usage"), dict)


def check_tools(tools: Sequence[Mapping[str, Any]]) -> None:
    """RequestRefused unless every tool has exactly the registry's shape."""
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping) or set(tool) != TOOL_KEYS:
            raise RequestRefused(
                f"tool {index}: not exactly {{name, description, input_schema}}"
                " (no server tool, no other field)"
            )
        name, description, schema = tool["name"], tool["description"], tool["input_schema"]
        if not (isinstance(name, str) and isinstance(description, str)):
            raise RequestRefused(f"tool {index}: name and description must be text")
        if not isinstance(schema, Mapping):
            raise RequestRefused(f"tool {index}: input_schema must be an object")


def hold_loggers() -> None:
    """Every logger of the three libraries at SDK_LOGGERS_MIN_LEVEL or above: the parents,
    and any child that was set lower."""
    for name in SDK_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.getEffectiveLevel(), SDK_LOGGERS_MIN_LEVEL))
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if not isinstance(logger, logging.Logger):
            continue  # a placeholder for a child not yet created
        if name.partition(".")[0] in SDK_LOGGERS and logging.NOTSET < logger.level:
            logger.setLevel(max(logger.level, SDK_LOGGERS_MIN_LEVEL))


def _call_error(error: anthropic.APIError) -> ModelCallError:
    if isinstance(error, anthropic.APITimeoutError):
        return ModelCallError("timeout")
    if isinstance(error, anthropic.APIConnectionError):
        return ModelCallError("connection")
    if isinstance(error, anthropic.APIStatusError):
        return ModelCallError("status", error.status_code)
    return ModelCallError("response")


def build_model_client(
    settings: Settings, *, transport: httpx2.BaseTransport | None = None
) -> AnthropicModelClient:
    """The client, or ModelClientRefused. transport is for tests (httpx2.MockTransport)."""
    check_start(settings, os.environ)
    if settings.anthropic_api_key is None:
        raise ModelClientRefused(f"{KEY_VARIABLE} is not set")
    hold_loggers()
    timeout = httpx2.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    http_client = httpx2.Client(
        transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
    )
    sdk = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        base_url=BASE_URL,
        max_retries=MAX_RETRIES,
        timeout=timeout,
        http_client=http_client,
        open_telemetry={"traces": False, "propagation": False},
    )
    return AnthropicModelClient(sdk, settings.model)


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise PriceUnknown(f"{key} is not a token count")
    return value


def cost_usd(model: str, usage: Mapping[str, Any]) -> Decimal:
    """The cost of one response's usage, in USD, exact."""
    price = PRICES.get(model)
    if price is None:
        raise PriceUnknown("no price for this model")
    # The table prices the standard tier and speed, without server tools (review finding
    # sec-6): priority, batch and fast are priced differently, server tools per request.
    if usage.get("service_tier") not in (None, "standard"):
        raise PriceUnknown("no price for this service tier")
    if usage.get("speed") not in (None, "standard"):
        raise PriceUnknown("no price for this speed")
    server_tools = usage.get("server_tool_use") or {}
    if not isinstance(server_tools, Mapping) or any(
        _count(server_tools, key) for key in server_tools
    ):
        raise PriceUnknown("no price for server tool use")
    geo = usage.get("inference_geo") or "global"
    multiplier = GEO_MULTIPLIERS.get(geo) if isinstance(geo, str) else None
    if multiplier is None:
        raise PriceUnknown("no price for this inference geography")
    written = _count(usage, "cache_creation_input_tokens")
    breakdown = usage.get("cache_creation")
    if breakdown:
        if not isinstance(breakdown, Mapping):
            raise PriceUnknown("cache_creation is not a breakdown")
        write_5m = _count(breakdown, "ephemeral_5m_input_tokens")
        write_1h = _count(breakdown, "ephemeral_1h_input_tokens")
        if write_5m + write_1h != written:
            raise PriceUnknown("the cache writes by duration do not add up to their total")
    else:
        # Without a breakdown every write is priced as the 5-minute kind, the default TTL.
        write_5m, write_1h = written, 0
    total = (
        _count(usage, "input_tokens") * price.input
        + write_5m * price.cache_write_5m
        + write_1h * price.cache_write_1h
        + _count(usage, "cache_read_input_tokens") * price.cache_read
        + _count(usage, "output_tokens") * price.output
    )
    return total * multiplier / PER_TOKEN
