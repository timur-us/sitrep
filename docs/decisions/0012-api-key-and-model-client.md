# ADR 0012: The API key and the model client

## Status

Accepted in outline (Timur, 2026-11-08: the M1 plan, PR 6); this text awaits Fable's security review with PR 6.

## Date

2026-11-09

## Context

SITREP (situation report) sends client data to Sonnet 5.5 on the direct Messages API (ADR 0003). The API key is the system's only secret (threat model, "Assets"). Read in `anthropic` 2.3.0:
- **The SDK reads its environment.** `ANTHROPIC_BASE_URL` redirects it; `ANTHROPIC_AUTH_TOKEN`, profiles under `ANTHROPIC_CONFIG_DIR` and workload-identity variables authenticate it; `ANTHROPIC_CUSTOM_HEADERS` adds headers; `ANTHROPIC_LOG` lowers its logging; `ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content` puts prompts into its spans. About forty names in all.
- **`DefaultHttpxClient` mounts proxy variables** whatever `trust_env` says.
- **At DEBUG** the SDK logs request options (the prompt) and response headers.
- **The key goes out as `Authorization: Bearer`.**
- **`PYDANTIC_SETTINGS_DEBUG`** makes `Settings` log every source's raw values, a key in `.env` included (PR 3, finding sec-3).

## Options

- **Let the SDK configure itself** (`Anthropic()`): little code. Any of those variables redirects, re-authenticates or logs the client unseen.
- **Refuse a list of variables:** misses the name the next SDK adds.
- **Refuse the prefix, build the HTTP client ourselves:** an exported `ANTHROPIC_*` stops the app, Claude Code's own included; we own the HTTP options.
- **Raw HTTP:** no SDK surface; we would rebuild typed errors, requests and ADR 0003's switch.

## Decision

The third option, in `app/harness/model_client.py`:
- **One factory** refuses, naming variables only: every `ANTHROPIC_*` in any case but the key, a missing key, a model without a price. The entry point checks the same before it binds; without a key the app starts.
- **The client.** The key is passed explicitly, so no profile is read; the base URL is a constant. Our httpx2 client has `trust_env=False` (no proxies, `SSL_CERT_*` or netrc), no redirects, no retries (the loop's). SDK spans and propagation are off; its loggers stay at INFO or above.
- **Errors** are a kind and an HTTP status. The SDK's exception, with the request and the body, is dropped, not chained.
- **Settings.** `ANTHROPIC_API_KEY` keeps its name (plan default 7) as a `SecretStr` (empty is none); a key that is not printable ASCII is refused unseen. `ANTHROPIC_MODEL` becomes `SITREP_MODEL`. `Settings` refuses to build while `PYDANTIC_SETTINGS_DEBUG` is set.
- **Imports.** `anthropic` and `httpx2` in that file only; `anthropic.lib` and the SDK's HTTP clients banned. The HTTP stack enters the lock as the SDK's; the Hub client stays out (ADR 0005).
- **Cost** is usage times a dated price table: Sonnet 5.5 only, 1.1× for US-only inference.
- **The key** (Timur): a Console workspace for SITREP alone with a spend limit he sets; one key, only in his `.env`, never in CI, the image or a GitHub secret. Rotation: new key, `.env` updated, old key revoked, at once on a suspected leak. Every live run starts with a cost estimate (plan default 17).

```yaml
base_url: https://api.anthropic.com
key_variable: ANTHROPIC_API_KEY
model_variable: SITREP_MODEL
default_model: claude-sonnet-5-5
refused_prefix: ANTHROPIC_
max_retries: 0
timeout_seconds: 120
connect_timeout_seconds: 5
prices_as_of: 2026-11-09
prices_usd_per_mtok:
  claude-sonnet-5-5: {input: "2", cache_write_5m: "2.50", cache_write_1h: "4", cache_read: "0.10", output: "10"}
```

## Consequences

- **Easier:** no variable moves the client, its key or its logs unseen; the spend limit caps a runaway loop or a leaked key; the factory is ADR 0003's place for the Foundry switch.
- **Harder:**
  - An exported `ANTHROPIC_*` refuses start, until it is unset.
  - Prices are by hand: a price change needs a dated edit here and in the code.
  - The workspace, key and spend limit are Timur's steps.
  - Each response waits for the whole answer, up to 120 seconds.
- **Watch:**
  - every SDK upgrade, through the source scan;
  - Haiku 5.5 (M2) needs its prompt-length tiers;
  - EU-only inference (ADR 0003);
  - streaming, if 120 seconds is too short.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 6 and default 7 (2026-11-08).
- `anthropic` 2.3.0 source, `_client.py` (credentials, base URL, custom headers, bearer auth), `_tracing.py`, `_utils/_logs.py`, `_base_client.py` (`DefaultHttpxClient`, the DEBUG records), `lib/credentials/` (read 2026-11-09).
- `httpx2` 2.16.0 and `httpcore2` 2.16.0 source, `trust_env` (read 2026-11-09).
- `pydantic-settings` 2.16.1 source, `main.py` (`_settings_log_debug`) and `utils.py` (read 2026-11-09).
- Anthropic, "Pricing", https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-11-09).
- PR #25, "Review", finding sec-3 (2026-11-09).
