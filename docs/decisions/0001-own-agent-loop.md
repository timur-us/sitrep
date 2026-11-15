# ADR 0001: Own agent loop on the Messages API

## Status

Accepted (Timur, 2026-10-28, in the M0 plan).

## Date

2026-10-29

## Context

SITREP (situation report) lets a model call four tools for an advisor. Two of them have effects (`draft_email`, `create_followup`) and may run only after the advisor approves, with a server-issued single-use token (`docs/brief.md`, threat-model path 4). Every turn needs a token budget, a turn limit (path 8) and a trace without prompt plaintext (CLAUDE.md rule 10). Those controls live in the loop, so it must be code we read and test, not someone else's runtime.

## Options

- **SDK tool runner** (`client.beta.messages.tool_runner`). Little code, but beta; Anthropic's page says to use the manual loop "when you need human-in-the-loop approval, custom logging, or conditional execution", which is all three of ours.
- **Claude Agent SDK.** A full runtime with file, shell and web tools that, without `setting_sources`, reads the Claude Code CLI's settings and CLAUDE.md files: the wrong default surface for a bank copilot.
- **Claude Managed Agents.** A hosted harness with a managed sandbox (beta); it moves the loop and state out of our process and traces, and lacks zero data retention.
- **Own loop on the Messages API.** More code, whose correctness is ours.

## Decision

Our own loop on the Messages API, in `app/harness/` (interfaces in M0, PR 6; the loop in M1). No Agent SDK, no tool runner, no Managed Agents.

### Amendment (PR 6, 2026-11-03)

After Fable's security review of PR 6, Timur decided:
- **Untrusted marker.** A READ result enters the context only through `wrap()`: `<untrusted tool="…" records="…">`, the text, `</untrusted>`. Text and attributes are HTML-escaped, so a document cannot close the marker; the cost: the model reads `&`, `<` and `>` as entities, which M2 measures.
- **Identity words.** An argument name is refused when, after NFKC, case folding and removing separators, it contains one of the plan's terms. Stricter than the plan's camelCase split, which lets `advisorid` pass; the cost: refusing names such as `username`.
- **`Proposal`** is the result of every DRAFT and SIDE_EFFECT tool: the id of a pending approval, nothing that approves it.
- **Error locations stay** in `CallError`, so the model sees which key was refused; traces record error types only (an M1 test).
- **Approval tokens:** ADR 0011.

### Amendments (PR 10, 2026-11-10; PR 12, 2026-11-11; PR 13, 2026-11-13)

- `app/harness/loop.py`: READ tools only; a block allowlist; 6 turns, 8 calls per turn, 24 per request, 150,000 tokens. `max_tokens` stops as "budget"; `pause_turn` fails. One retry, not for timeouts. No refusal fallback (ADR 0003).
- PR 12: `ToolFailed`'s reviewed kinds reach the model; `GuardError` and database failures end the request. The route stops model calls after 90 s ("budget").
- PR 13: argument types from an allowlist, no validators or methods; arguments past depth 4 or 32 KiB, or holding control characters, refused before pydantic.

## Consequences

- **Easier:** the approval gate, the provenance wrapper, the output policy and the canary scan sit between the model and every tool call, where tests can reach them. Traces carry exactly what we allow.
- **Harder, and ours to get right:**
  - `stop_reason` handling, `refusal` (an HTTP 200, not an error) and `model_context_window_exceeded` among them;
  - the ordering rule: after a `tool_use`, the user turn's `tool_result` blocks come first;
  - retries, budgets and the turn limit;
  - no forced `tool_choice` on Sonnet 5.5, whatever the thinking settings (400 for `any` and `tool`); Haiku 5.5 (the guard layer) accepts it.
- **Watch:** if the tool runner leaves beta with an approval hook, re-check this ADR.

## Sources

- Anthropic, "Tool runner (SDK)", https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-runner (accessed 2026-10-29; page marked beta).
- Anthropic, "Use Claude Code features in the SDK", https://code.claude.com/docs/en/agent-sdk/claude-code-features (accessed 2026-10-29), and "Agent SDK overview", https://code.claude.com/docs/en/agent-sdk/overview (accessed 2026-10-29).
- Anthropic, "Claude Managed Agents overview", https://platform.claude.com/docs/en/managed-agents/overview (accessed 2026-10-29).
- Anthropic, "Stop reasons and fallback", https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons (accessed 2026-10-29).
- Anthropic, "Handle tool calls", https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls (accessed 2026-10-29).
- Anthropic, "Define tools" (forced `tool_choice` by model), https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools (accessed 2026-10-29).
- `docs/plans/m0-foundation.md`, "ADR 0003" research notes (2026-10-28).
- M1 PR 10, the loop: stop reasons mapped to the API contract's four stops (`max_tokens` and `model_context_window_exceeded` to "budget", `pause_turn` refused since no server tool is offered); one retry for transient failures; the message-block allowlist that PR 6 made a merge gate; assistant turns passed back unchanged, as preserved thinking requires. `docs/plans/m1-mvp-core.md`, PR 10, and the Anthropic API reference bundled with Claude Code (read 2026-11-10).
- M1 PR 12, `POST /api/ask`: Timur's answer 1 on #33 (a route-level deadline; the loop stays without a clock, so the route wraps the model client and no call starts 90 s or more after the route began); the handovers of #32 and #33 (`ToolFailed`'s kind, a fixed word from `handlers.TOOL_FAILURES`, reaches the model and the span; a `GuardError` from a handler ends the request as `LoopError("guard")`, an `OperationalError` as `"database"`); and the plan's rate limit per verified advisor, in process: 10 asks started in any 60 s, one in flight. `docs/plans/m1-mvp-core.md`, PR 12 (read 2026-11-11).
- M1 PR 13, the registry: plan default 11 (a type allowlist, not a list of refused features), the PR 6 handovers sec-2 (validators, schema overrides, `RootModel`) and sec-11 (depth and size caps, `too_deep` and `too_large`, before pydantic), and #32's sec-9 (a NUL reached the database as a `DataError`; C0, DEL and C1 control characters are now refused, tab, LF and CR pass). `docs/plans/m1-mvp-core.md`, PR 13 and its amendment of 2026-11-13 (read 2026-11-13).
- Detail moved here from the body in M1 PR 13, to keep the page: the Agent SDK reads the CLI's user, project and local settings; the stop reasons are `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`, `pause_turn`, `refusal` and `model_context_window_exceeded`. Same sources as above (accessed 2026-10-29).
