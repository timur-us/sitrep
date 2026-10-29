# ADR 0007: Tracing and logs without plaintext

## Status

Proposed (M0 PR 3; accepted with the merge).

## Date

2026-10-29

## Context

SITREP (situation report) will carry client data and prompts through every request. CLAUDE.md rule 10: traces carry hashes, lengths, ids and policy decisions, never prompt text; Eve, the passive reader, is why. The defaults leak before any prompt exists:

- the FastAPI instrumentation records the URL with its query, the Host header, user agent and client address, and exception messages and stack traces; environment variables make it copy headers and raw methods;
- it adopts an inbound `traceparent`;
- uvicorn's access log writes path and query, its error log full tracebacks.

## Options

- **Configure the instrumentation** (hooks, excluded URLs, header lists). Each new default or environment variable reopens a leak.
- **Allowlist at the exporter.** Every span passes one filter before it leaves the process; what is not approved is dropped. New attributes are lost until reviewed.
- **No tracing until M3.** No leak, but no trace id for a support case, and tracing retrofitted onto M1's loop.

## Decision

- **`AllowlistExporter`** wraps the real exporter (console in M0). It keeps the attribute keys `http.method` (standard methods only, else `_OTHER`), `http.route`, `http.status_code`, `http.scheme`, `http.flavor`; the event `exception` with `exception.type` only; the resource key `service.name`; the status code without its description. Links and trace state are dropped. A span name survives only as method plus route template, otherwise it becomes `redacted`. The tests write the lists out, so widening one is a reviewed change.
- **Explicit provider, no globals for traces.** The provider goes to the instrumentation directly; its resource ignores `OTEL_RESOURCE_ATTRIBUTES`. The global propagator is an empty one: inbound trace context is never adopted. Spans export synchronously (`SimpleSpanProcessor`).
- **`X-Trace-Id`** on every response, 500 included, so a support case can quote it. Ids come from `secrets`, not from the SDK's `random`, whose state the ids would reveal.
- **Logs.** No access log, `proxy_headers` off, no server header. A redacting filter on uvicorn's loggers and on the handler keeps the message template, drops its arguments, and turns an exception into its type (plus the symbolic errno for an `OSError`) and the trace id. The handler never prints its own traceback on a failed write, since that traceback's context is the exception being logged.
- **`TRACE_PLAINTEXT=1`** (exactly `"1"`) sends spans to the console unfiltered. It is refused, before the server starts, unless the exporter is the console and the entry point binds exactly `127.0.0.1`. The image binds `0.0.0.0` by default and refuses it; `-e HOST=127.0.0.1` would pass the check. Logs stay redacted either way.

## Consequences

- **Easier:** a new library, semantic-convention change or environment variable cannot add plaintext to spans without a reviewed edit; the real-server test proves it with a canary in query, headers, cookie, body, path and exception message.
- **Harder:** debugging sees less. Exception messages and query strings are gone from traces and logs. Locally, `TRACE_PLAINTEXT=1` restores the spans; logs have no such switch. Log lines show `%d`-style templates without their values.
- **Watch:**
  - the allowlist uses the instrumentation's pre-stable HTTP names; opting into the stable conventions drops those attributes until the list is reviewed;
  - message templates are trusted to be constants: an f-string with request content, asyncio's task-exception message, and the thread and unraisable hooks get through (no M0 path; M1's loop needs them closed);
  - M3's Application Insights exporter replaces the console behind the same allowlist, batched, and needs an image guard for `TRACE_PLAINTEXT`;
  - M1 approves prompt hashes and lengths as attributes, by name.

## Sources

- CLAUDE.md, rule 10 (2026-10-27).
- `docs/threat-model.md`, Eve and the audit trail (2026-10-27).
- `docs/plans/m0-foundation.md`, "Tracing and logs" and PR 3 (2026-10-28).
- opentelemetry-instrumentation-fastapi and -asgi 0.66b1 source, `instrument_app` and `OpenTelemetryMiddleware` (read 2026-10-29).
- uvicorn 0.54.0 source, `Config.configure_logging` and the h11 protocol's access log (read 2026-10-29).
- W3C Trace Context, https://www.w3.org/TR/trace-context/ (accessed 2026-10-29).
