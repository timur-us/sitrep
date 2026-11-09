# ADR 0012: The API key and the model client

## Status

Accepted in outline (Timur, 2026-11-08: the M1 plan, PR 6); this text awaits Fable's security review with PR 6.

## Date

2026-11-09

## Context

SITREP (situation report) sends client data to Sonnet 5.5 on the direct Messages API (ADR 0003). The API key is the system's only secret (threat model, "Assets"). Read in `anthropic` 2.3.0:
- **The SDK reads its environment** (about forty names): `ANTHROPIC_BASE_URL` redirects it; a token, profiles and workload identity authenticate it; `ANTHROPIC_CUSTOM_HEADERS` adds headers; `ANTHROPIC_LOG` lowers its logging; a content mode puts prompts into spans.
- **`DefaultHttpxClient` mounts proxy variables** whatever `trust_env` says.
- **At DEBUG** the SDK logs request options (the prompt) and response headers.
- **The key goes out as `Authorization: Bearer`.**
- **`PYDANTIC_SETTINGS_DEBUG`** makes `Settings` log every source's raw values, a key in `.env` included (PR 3, finding sec-3).

## Options

- **Let the SDK configure itself** (`Anthropic()`): little code; any of those variables moves the client unseen.
- **Refuse a list of variables:** misses the next SDK's names.
- **Refuse the prefix, build the HTTP client ourselves:** any exported `ANTHROPIC_*` stops the app; we own the HTTP options.
- **Raw HTTP:** we would rebuild typed errors, requests and ADR 0003's switch.

## Decision

The third option, in `app/harness/model_client.py`:
- **One factory** refuses, by name only: any `ANTHROPIC_*` but the key, no key, a model without a price. `python -m app` also refuses before binding, and refuses an exported key, which would beat `.env`'s.
- **The client.** Explicit key (no profile read), constant base URL. Our httpx2 client: `trust_env=False` (no proxies, `SSL_CERT_*` or netrc), no redirects, no retries (the loop's). SDK spans and propagation off; the three libraries' loggers held at INFO or above. Only registry-shaped tools are sent, so no server tool.
- **Errors** are a kind and an HTTP status; the SDK's exception (request, body) is dropped, not chained.
- **Settings.** The key keeps its name (default 7), a `SecretStr`; empty is none, a bad shape refused unseen. `SITREP_MODEL` replaces `ANTHROPIC_MODEL`. `PYDANTIC_SETTINGS_DEBUG` refuses `Settings`.
- **Imports.** `anthropic` and `httpx2` in that file only; `anthropic.lib`, the other clients and the SDK's HTTP clients banned. The HTTP stack enters the lock as the SDK's; the Hub client stays out (ADR 0005).
- **Cost:** usage times a dated table, Sonnet 5.5, standard tier only.
- **The key** (Timur): a Console workspace for SITREP alone with his spend limit; one key, only in his `.env`, never in CI, the image or a GitHub secret. Rotation: new key, `.env` updated, old key revoked, at once on a suspected leak. A cost estimate before every live run (default 17).

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
geo_multipliers: {global: "1", us: "1.1"}
sdk_loggers: [anthropic, httpx2, httpcore2]
sdk_loggers_min_level: INFO
```

## Consequences

- **Easier:** no variable moves the client, key or logs unseen; the spend limit caps a runaway loop or leaked key; the factory hosts ADR 0003's switch.
- **Harder:**
  - An exported `ANTHROPIC_*` refuses start until unset.
  - Seven packages enter the lock (`httpx2`, `httpcore2`, `truststore`, `jiter` among them).
  - We rely on SDK internals (`open_telemetry=`, `DefaultHttpxClient`, the DEBUG records); tests read the source, yet an upgrade can change them.
  - The logger hold is process-wide; a logger lowered later stays lowered.
  - Without retries a 429 or 529 fails the call until the loop (PR 10) retries.
  - Prices are by hand; a priority, batch, fast or server-tool response cannot be priced.
  - Answers arrive whole, within 120 seconds.
- **Watch:** SDK upgrades (the source scans); Haiku 5.5's tiers (M2); TLS roots, the OS store on macOS; how M3 passes the key, since an exported one is refused; EU-only inference; streaming.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 6 and default 7 (2026-11-08).
- `anthropic` 2.3.0 source, `_client.py` (credentials, base URL, custom headers, bearer auth), `_tracing.py`, `_utils/_logs.py`, `_base_client.py` (`DefaultHttpxClient`, the DEBUG records), `lib/credentials/` (read 2026-11-09).
- `httpx2` 2.16.0 and `httpcore2` 2.16.0 source, `trust_env` (read 2026-11-09).
- `pydantic-settings` 2.16.1 source, `main.py` (`_settings_log_debug`) and `utils.py` (read 2026-11-09).
- Anthropic, "Pricing", https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-11-09).
- PR #25, "Review", finding sec-3 (2026-11-09).
