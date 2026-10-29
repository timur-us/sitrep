# ADR 0007: Tracing and logs without plaintext

## Status

Proposed (M0 PR 3; accepted with the merge).

## Date

2026-10-29

## Context

SITREP (situation report) will carry client data and prompts through every request. CLAUDE.md rule 10 says traces carry hashes, lengths, ids and policy decisions, never prompt text; Eve, the passive reader of logs and proxies, is why (threat model). The defaults leak before any prompt exists:

- the FastAPI instrumentation records the full URL with the query string, the Host header, the user agent and the client address; it writes exception messages and stack traces into span events and into the status description; environment variables can make it copy request headers;
- it adopts an inbound `traceparent`, so a caller can choose the trace id;
- uvicorn's access log writes path and query, and its error log writes full tracebacks.

## Options

- **Configure the instrumentation** (hooks, excluded URLs, header lists). Each new default or environment variable reopens a leak.
- **Allowlist at the exporter.** Every span passes one filter before it leaves the process: what is not approved is dropped. New attributes are lost until reviewed, which is the point.
- **No tracing until M3.** No leak, but no trace id for a support case, and tracing retrofitted onto M1's loop.

## Decision

- **`AllowlistExporter`** wraps the real exporter (console in M0). It keeps the attribute keys `http.method`, `http.route`, `http.status_code`, `http.scheme`, `http.flavor`; the event `exception` with `exception.type` only; the resource key `service.name`; the status code without its description. Links are dropped. A span name survives only if it is the method plus the route template, otherwise it becomes `redacted`. The lists are written out in the tests, so widening one is a reviewed change.
- **Explicit provider, no globals for traces.** The tracer provider goes to the instrumentation directly and its resource ignores `OTEL_RESOURCE_ATTRIBUTES`. The global propagator is an empty one: inbound trace context is never adopted. Spans export synchronously (`SimpleSpanProcessor`), which is enough for a local console.
- **`X-Trace-Id`** on every response, 500 included, so a support case can quote it.
- **Logs.** No access log, `proxy_headers` off, no server header. A redacting filter on uvicorn's loggers and on the handler keeps the message template, drops its arguments, and turns an exception into its type (plus the symbolic errno for an `OSError`) and the trace id. The handler never prints its own traceback on a failed write, since that traceback's context is the exception being logged.
- **`TRACE_PLAINTEXT=1`** (exactly `"1"`) sends spans to the console unfiltered. It is refused, before the server starts, unless the exporter is the console and the entry point binds exactly `127.0.0.1`. The image binds `0.0.0.0`, so it can never run with it. Logs stay redacted either way.

## Consequences

- **Easier:** a new library, semantic-convention change or environment variable cannot add plaintext to spans without a reviewed edit; the real-server test proves it with a canary in query, headers, cookie, body, path and exception message.
- **Harder:** debugging sees less. Exception messages and query strings are gone from traces and logs. Locally, `TRACE_PLAINTEXT=1` restores the spans; logs have no such switch. Log lines show `%d`-style templates without their values.
- **Watch:**
  - the allowlist uses the instrumentation's pre-stable HTTP names; opting into the stable conventions drops those attributes until the list is reviewed;
  - message templates are trusted to be constants; a library that logs an f-string with request content would get through;
  - M3's Application Insights exporter replaces the console behind the same allowlist, batched;
  - M1 approves prompt hashes and lengths as attributes, by name.

## Sources

- CLAUDE.md, rule 10 (2026-10-27).
- `docs/threat-model.md`, Eve and the audit trail (2026-10-27).
- `docs/plans/m0-foundation.md`, "Tracing and logs" and PR 3 (2026-10-28).
- opentelemetry-instrumentation-fastapi and -asgi 0.66b1 source, `instrument_app` and `OpenTelemetryMiddleware` (read 2026-10-29).
- uvicorn 0.54.0 source, `Config.configure_logging` and the h11 protocol's access log (read 2026-10-29).
- W3C Trace Context, https://www.w3.org/TR/trace-context/ (accessed 2026-10-29).
