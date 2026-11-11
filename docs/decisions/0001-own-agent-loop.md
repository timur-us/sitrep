# ADR 0001: Own agent loop on the Messages API

## Status

Accepted (Timur, 2026-10-28, in the M0 plan).

## Date

2026-10-29

## Context

SITREP (situation report) lets a model call four tools for an advisor. Two of them have effects (`draft_email`, `create_followup`) and may run only after the advisor approves, with a server-issued single-use token (`docs/brief.md`, threat-model path 4). Every turn needs a token budget and a turn limit (path 8), and a trace without prompt plaintext (CLAUDE.md rule 10). The loop is where those controls live, so it has to be code we read and test, not configuration of someone else's runtime.

## Options

- **SDK tool runner** (`client.beta.messages.tool_runner`). Little code. It is beta, and Anthropic's own page says to use the manual loop "when you need human-in-the-loop approval, custom logging, or conditional execution", which is all three of ours.
- **Claude Agent SDK.** A full agent runtime. It ships file, shell and web tools, and when `setting_sources` is not passed it reads the same user, project and local settings and CLAUDE.md files as the Claude Code CLI. For a bank copilot that is the wrong default surface: we would spend the effort switching things off.
- **Claude Managed Agents.** A hosted harness with a managed sandbox (beta). It moves the loop and its state out of our process and our traces, and it is not eligible for zero data retention.
- **Own loop on the Messages API.** More code, and we own its correctness.

## Decision

Our own loop on the Messages API, in `app/harness/` (interfaces in M0, PR 6; the loop in M1). No Agent SDK, no tool runner, no Managed Agents.

### Amendment (PR 6, 2026-11-03)

Timur's decisions after Fable's security review of PR 6:
- **Untrusted marker.** A READ result enters the context only through `wrap()`: `<untrusted tool="…" records="…">`, the text, `</untrusted>`. Text and attributes are HTML-escaped, so a document cannot close the marker; the cost is that the model reads `&`, `<` and `>` as entities. M2 measures the rendering.
- **Identity words.** An argument name is refused when, after NFKC, case folding and removing separators, it contains one of the plan's terms. That is stricter than the plan's camelCase split, which lets `advisorid` pass; the cost is refusing names such as `username`.
- **`Proposal`** is the result of every DRAFT and SIDE_EFFECT tool: the id of a pending approval, nothing that approves it.
- **Error locations stay** in `CallError`, so the model sees which key was refused; traces record error types only (an M1 test).
- **Approval tokens:** ADR 0011.

### Amendment (PR 10, 2026-11-10)

- `app/harness/loop.py`: READ tools only; a block allowlist; 6 turns, 8 calls per turn, 24 per request, 150,000 tokens. `max_tokens` stops as "budget"; `pause_turn` fails. One retry, not for timeouts. No refusal fallback (ADR 0003).

## Consequences

- **Easier:** the approval gate, the provenance wrapper, the output policy and the canary scan sit between the model and every tool call, where tests can reach them. Traces carry exactly what we allow.
- **Harder, and ours to get right:**
  - `stop_reason` handling: `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`, `pause_turn`, `refusal` (an HTTP 200, not an error) and `model_context_window_exceeded`;
  - the ordering rule: in the user turn after a `tool_use`, the `tool_result` blocks come first;
  - retries, budgets and the turn limit;
  - no forced `tool_choice` on Sonnet 5.5: `any` and `tool` return 400, whatever the thinking settings. Haiku 5.5 accepts it, which matters for the guard layer only.
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
