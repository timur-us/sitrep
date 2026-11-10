# ADR 0007: Tracing and logs without plaintext

## Status

Accepted (Timur, 2026-10-29). Amended in M1 PR 5 and M1 PR 9 (Sources).

## Date

2026-10-29

## Context

SITREP (situation report) will carry client data and prompts through every request. CLAUDE.md rule 10: traces carry hashes, lengths, ids and policy decisions, never prompt text; Eve, the passive reader, is why. The defaults leak before any prompt exists:

- the FastAPI instrumentation records the URL and query, Host, user agent, client address, exception messages and stack traces; environment variables make it copy headers and raw methods;
- it adopts an inbound `traceparent`;
- uvicorn's access log writes path and query, its error log full tracebacks.

## Options

- **Configure the instrumentation** (hooks, excluded URLs, header lists). Each new default reopens a leak.
- **Allowlist at the exporter.** Every span passes one filter before it leaves the process; what is not approved is dropped. New attributes are lost until reviewed.
- **No tracing until M3.** No leak, but no trace id for support, and tracing retrofitted later.

## Decision

- **`AllowlistExporter`** wraps the real exporter (console in M0). It keeps the attribute keys `http.method` (standard methods only, else `_OTHER`), `http.route`, `http.status_code`, `http.scheme`, `http.flavor`; the event `exception` with `exception.type` only; the resource key `service.name`; the status code without its description. Links and trace state are dropped. A span name survives only as method plus route template, otherwise it becomes `redacted`. The tests write the lists out, so widening one is a reviewed change.
- **Explicit provider, no globals for traces.** The provider goes to the instrumentation directly; its resource ignores `OTEL_RESOURCE_ATTRIBUTES`. The global propagator is an empty one: inbound trace context is never adopted. Spans export synchronously (`SimpleSpanProcessor`).
- **`X-Trace-Id`** on every response, 500 included, so a support case can quote it. Ids come from `secrets`, not from the SDK's `random`, whose state the ids would reveal.
- **Logs.** No access log, `proxy_headers` off, no server header. A redacting filter on uvicorn's loggers and on the handler keeps the message template, drops its arguments, and turns an exception into its type (plus the symbolic errno for an `OSError`) and the trace id. The handler never prints its own traceback. `python -m app` replaces Python's thread, unraisable, main-thread and warning hooks, the lifespan the loop's asyncio handler, and a filter guards asyncio's own logger: a fixed message, the type, the trace id.
- **Grounding** (M1 PR 9): its report holds counts and cited ids a tool returned; never figures, source text or model-written ids.
- **`TRACE_PLAINTEXT=1`** (`SITREP_TRACE_PLAINTEXT`, exactly `"1"`) sends spans to the console unfiltered. It is refused, before the server starts, unless the exporter is the console and the entry point binds exactly `127.0.0.1`, and while any `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` variable is set (any case or value): captured headers would print the session cookie. The image binds `0.0.0.0` and refuses it. Logs stay redacted either way.

## Consequences

- **Easier:** a new library, convention or environment variable cannot add plaintext to spans unreviewed; a real-server test proves it with canaries.
- **Harder:** debugging sees less: no exception messages or query strings in traces or logs. Locally, plaintext restores the spans, never the logs.
- **Watch:**
  - the allowlist uses the instrumentation's pre-stable HTTP names; opting into the stable conventions drops those attributes until the list is reviewed;
  - message templates are trusted to be constants: an f-string with request content gets through;
  - M3's Application Insights exporter replaces the console behind the same allowlist, batched, and needs an image guard for `SITREP_TRACE_PLAINTEXT`;
  - M1 approves prompt hashes and lengths as attributes, by name.

## Sources

- M1 PR 5: the plaintext refusal under header capture and the hooks from Watch. Timur's answer 5 to the PR 7 questions, `docs/plans/m0-foundation.md` (amendment recorded in PR 9), and `docs/plans/m1-mvp-core.md`, PR 5 (2026-11-08).
- M1 PR 9: the grounding report, built for traces; an unknown cited id is model output, so it is counted and dropped. Its trace keys are approved in PR 10. `docs/plans/m1-mvp-core.md`, PR 9 and default 14 (2026-11-08).
- CLAUDE.md, rule 10 (2026-10-27).
- `docs/threat-model.md`, Eve and the audit trail (2026-10-27).
- `docs/plans/m0-foundation.md`, "Tracing and logs" and PR 3 (2026-10-28).
- opentelemetry-instrumentation-fastapi and -asgi 0.66b1 source, `instrument_app` and `OpenTelemetryMiddleware` (read 2026-10-29).
- uvicorn 0.54.0 source, `Config.configure_logging` and the h11 protocol's access log (read 2026-10-29).
- W3C Trace Context, https://www.w3.org/TR/trace-context/ (accessed 2026-10-29).
