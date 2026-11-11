# M1 — MVP core, local: plan

Date: 2026-11-08 · Status: accepted by Timur with his answers to four questions and Fable's three additions (merging, the ADR design reads, the cache fallback; folded in below) · Owner: Timur · Amended: 2026-11-10 (the red team before the approval path) · Milestone: M1, days 4–9 of `docs/brief.md`, re-baselined (question 4)

## Context

SITREP (situation report) ends M0 with a foundation that has never served a real document: row-level security, the pool guard, the registry and the dev login are tested on planted rows and a test-local handler, nothing calls a model, and the eval and red-team gates score nothing (`docs/reports/m0.md`). M1 builds the MVP core the brief asks for:
- retrieval with row-level security over the synthetic corpus;
- the agent loop with the four tools;
- the approval gate;
- the UI, served by the app;
- evals v1, recorded;
- the first red-team case, unhardened, with a measured success rate.

M1 is local: no Azure, no OIDC, no deploy. Every row of the M0 report's "M1 handovers" lands in a named PR below or is deferred with a reason ("Handover coverage"). The two ADRs come first (the loader, approval tokens), then the `SITREP_` prefix for the database URLs as the first infrastructure PR.

**The end-to-end read slice merges first, and it is M1's cut line** (question 4). It runs from the loader, the ONNX embedder and retrieval to the loop with the two READ tools, and to an answer with sources through the dev login. Approval and the two proposal tools follow, then eval and red-team breadth. Since the amendment of 2026-11-10, evals v1 and the first red-team case come right after the slice, and approval follows them.

The outcome that matters: an advisor asks "What do I need to know before the call with Weber?" and gets a cited answer built from her own mandates only, with a trace id, the cost and the grounding result; a draft or a follow-up exists only after she approves it; and the EchoLeak e-mail's success rate is measured before any M2 hardening.

## Decisions taken (Timur, 2026-11-08)

### Asked

| # | Question | Answer | What follows |
|---|---|---|---|
| 1 | PR 8 sec-2 (recipients per mandate) and sec-3 (exact-URL links) hand M1 two allowlists, while the brief puts the output policy in M2, so that M1 can measure the unhardened rate. | Data and cases now, enforcement in M2 (the recommendation). | PR 7 loads recipients per mandate under row-level security and the exact product URLs; PR 17 records the cases unhardened; M2's output policy enforces both, measured against M1's baseline. |
| 2 | Who writes the corpus? The app role is SELECT-only, and FORCE binds the owner. | The migrator as owner, one advisor per transaction (the recommendation), with two conditions (given in German, translated): "the loader helper in session.py must be unreachable from app/ request code, with the same scan as for issue()", and "as a Watch item for M3: in Azure the loader gets its own identity, separate from the migrator". | PR 7 carries the reachability scan (modelled on `tests/test_identity.py`) and a runtime role check; ADR 0010 carries the M3 Watch item; "Left out" lists it under M3. |
| 3 | What do the offline gates replay? | Score transcripts (the recommendation). | ADR 0014 (PR 16): a live run, by hand, records the transcript with its prompt, tool-schema and corpus hashes; CI scores it deterministically and fails a stale one. No database, model or key in the `evals` and `redteam` jobs. Live runs log in through the dev login, in-process. |
| 4 | Schedule: about 54 build hours and a critical path of about 31 hours (the draft's order, before the slice came first), against the brief's days 4–9. | "Re-baseline with the full M1 scope; M4 absorbs the time. But order the PR sequence so that an end-to-end slice merges first as M1's cut line: loader, ONNX embedder, retrieval, the loop with the two READ tools, an answer with sources through the dev login. Then approval and the two proposal tools, then eval and red-team breadth. Mark that cut line in the plan." | The sequence below. PRs 1–12 are the slice; the cut line follows PR 12. The demo moves: the cut-line hand check after PR 12 is the first demo (about day 14), and the full demo comes at M1's close (about day 21; about day 24 after the amendment of 2026-11-10). |

### Defaults (recommended in the draft, accepted with the plan on 2026-11-08; a later change is a plan amendment)

| # | Topic | Default | Why |
|---|---|---|---|
| 5 | Runtime-written text | Drafts and follow-ups live in their own tables with origins `agent_draft` and `agent_followup`, trust `untrusted`, and are never retrieved in M1. | Timur's answer to PR 8 sec-1. A draft carrying injected text cannot re-enter a later context. M4's poisoning path revisits it. |
| 6 | The real embedder in CI | `make model` becomes a prerequisite of `lint-test` (and of `load`): about 490 MB from Hugging Face at the pinned revision, sha256-checked, with no cache. No `ci.yml` edit. **Fallback** (Fable's addition): if `lint-test` grows past three minutes, a cache whose entry is sha256-verified after every restore. | This deviates from ADR 0005's "cached": a cache needs a new pinned action, and cache entries are not content-verified. Amended in PR 4, with the fallback and its trigger. |
| 7 | Model settings | `ANTHROPIC_API_KEY` keeps its name; `ANTHROPIC_MODEL` becomes `SITREP_MODEL`. | The secret hook (`.claude/hooks/block_secrets.py`) and the GitHub checklist key on `ANTHROPIC_API_KEY`. Renaming it would weaken a guardrail only Timur may change. |
| 8 | ADR PRs | Docs class: spec-reviewer plus security-reviewer. **Fable reads ADR 0010 and ADR 0011 before their merge**, as a short design review, even though they are docs class (Fable's addition). | A design flaw costs less to fix in the ADR than in the code that follows it. |
| 9 | The UI PRs | Sensitive. | They render untrusted model output: the image and link exfiltration paths go through them. |
| 10 | Streaming | None in M1: one JSON response per question. | Together with the invalidated handle (PR 8), it closes PR 5b sec-5 for M1. |
| 11 | Registry hardening (PR 6 sec-2) | A type allowlist (closed world) instead of a list of refused features. | A denylist misses the next pydantic feature. |
| 12 | Trust | A STORED generated column from `origin`; no writer can set it. | The type-only trust rule, enforced by the schema. |
| 13 | `hnsw.iterative_scan` | Set transaction-locally in `app/db/session.py`. | No change to `db/init`, `ROLE_SETTINGS` or the pins, and no `make down`. |
| 14 | Grounding checks | The number guard and the citation check report (UI, trace counts, eval metric); they do not block in M1. | Blocking is a policy decision, M2's. |
| 15 | granite-r2 benchmark | Deferred to M2. PR 4 measures e5's CPU speed and runs a recall sanity check. | It doubles the model download and changes nothing unless e5 fails its bar. |
| 16 | Fail-first for docs | ADR PRs carry a machine-readable yaml block and a docs test (as ADR 0005 does). PR 0 is exempt from rule 2, as in M0. | |
| 17 | Live runs | Timur runs them himself (`! …`) after the agent's cost estimate. | CLAUDE.md says "live runs only by hand", and the secret hook blocks agent commands that name the key or `.env`. |
| 18 | The merge checklist | Dropped: `docs/runbooks/merge-checklist.md` does not exist and will not (Timur, 2026-11-08). This closes item 4 of the M0 report's "What Timur has to do". | |

### Amendment (Timur, 2026-11-10): the red team before the approval path

PR 16 (recorded runs, evals v1) and PR 17 (red team v1, unhardened) may be built and merged right after PR 12, before PRs 13 to 15. The reason: the EchoLeak case needs only the read path. The e-mail sits in Weber's mailbox, `search_client_docs` returns it, and the attack succeeds when the canary reaches the answer. Measuring that rate does not have to wait for the approval gate.

The two paragraphs above are Timur's decision. The rest of this section **was derived by the agent and is proposed for Timur to confirm**: his merge of the amendment PR confirms it, and his answers on that PR override it. It is applied in the sections below.
- **The first recordings run at prompt `v1`**, with the two READ tools. The live steps of PR 16 and PR 17 run after PR 12, not after PR 15.
- **Some cases need a proposal tool, so they land with PR 14:**
  - the eval cases for a draft e-mail and a follow-up;
  - EchoLeak's `draft_email` form (body and recipient);
  - the draft to an address outside the mandate (sec-2).

  A committed case without a recording fails the gate, so these cases cannot sit in PR 16 or PR 17 ahead of their tools. PR 14 adds them and raises both gates' `min_cases` to match.
- **PR 14 records both suites again, at `v2`.** It raises `PROMPT_VERSION` to `v2` and adds two tools to the registry, so the stale check fails every `v1` recording. PR 14 therefore carries:
  - the cases above;
  - a second recording of both suites by Timur, after the agent's cost estimate. This is his third live run in all.

  Until those recordings are committed, PR 14's `evals` and `redteam` jobs stay red, and the PR stays a draft.
- **The `v1` recordings stay in the repository** as evidence, in `evals/recorded/v1/` and its red-team counterpart. The gates score only the current prompt version's directory; ADR 0014 (PR 16) fixes that rule. `redteam/results/m1.md` keeps the `v1` read-path rate and adds the `v2` rate. **M2 measures against the `v2` rate,** because M2 hardens the full tool set.
- **PR 13 merges after PR 17 and before PR 14,** and it must leave `registry.schema()` unchanged. If it changes the schema, it has to record again as well.
- **The database chain** becomes 3 → 7 → 8 → 12 → 16's live step → 17 → 14 (with the `v2` recording) → 15 → 18.
- **Timur's Bash-side guard decision** (before PR 17) is now due about 24.5 hours in, after PR 16.
- **ADR numbers stay:** 0014 is still PR 16's. PRs 13–15 add amendments only.
- **The cost.** On the critical path, M1 closes about 6 hours later, at about day 24 instead of day 21, and the unhardened EchoLeak rate arrives about 3.5 hours earlier ("Time budget"). The build hours were not re-estimated: the proposal cases move from PR 16 and PR 17 to PR 14, and the table keeps each PR's figure, so the total stays at about 56.5 hours.

## Defaults that shape every PR

- **Branches** are `m1/NN-short-name`, each in its own worktree; nothing is pushed to `main`.
- **Settings** keep M0's rules: no implicit dotenv, and flags on only for exactly `"1"`. After PR 3, every name is `SITREP_`-prefixed except `ANTHROPIC_API_KEY` (default 7).
- **SQL** stays bind-only: every statement is a literal or a constant in `app/db/queries.py`, and `set_config` stays in `app/db/session.py` alone.
- **Identity** comes only from the login:
  - the app, through `current_advisor()`;
  - the live runner, through the dev login in-process (PR 16);
  - the loader, not at all: it writes data ownership from the corpus, as the owner, under FORCE, through a helper that request code cannot reach (question 2).
- **Untrusted.** Every tool result enters the context through `wrap()`; every runtime-written text is `untrusted`; attack documents carry the `rt-` id prefix.
- **Corpus convention.** Cases and attacks name the cast, the bank and the domains through placeholders only, under M0's runner rules.
- **Traces** carry ids, hashes, lengths, counts, error types and policy decisions, approved by name in `ALLOWED_KEYS` (ADR 0007).
- **New dependencies** pass the 7-day release age, the lock test and pip-audit. Each enters `tests/test_import_allowlist.py` with a reason; one that can reach the network gets a single-module rule.

## PR sequence

| # | Branch | PR | Class | DB | Depends on | Build h |
|---|---|---|---|---|---|---|
| 0 | `plan/m1` | This plan | docs | no | — | 1.0 |
| 1 | `m1/01-loader-adr` | ADR 0010: the loader, origin and trust, product sheets, attack ids | docs, + Fable's design read | no | 0 | 1.0 |
| 2 | `m1/02-approval-adr` | ADR 0011: approval tokens | docs, + Fable's design read | no | 0 | 1.0 |
| 3 | `m1/03-db-url-prefix` | `SITREP_` prefix for both database URLs | **sensitive** (app/db) | yes | 1, 2 (merge order) | 1.5 |
| 4 | `m1/04-embedder` | ONNX embedder, `make model`, token chunker | **sensitive** (gate path, supply chain) | no | 3 (merge order) | 3.5 |
| 5 | `m1/05-http-traces` | Host check without the shim, plaintext refused with OTel header capture, exception hooks | **sensitive** (app/auth, logging) | no | 0 | 2.5 |
| 6 | `m1/06-model-client` | Model client, ADR 0012 (API key workspace and spend limit) | **sensitive** (model client) | no | 3 | 3.0 |
| 7 | `m1/07-loader` | Loader, origin and trust columns, product sheets, recipients | **sensitive** (loader, app/db, gate path) | yes | 1, 3, 4 | 4.5 |
| 8 | `m1/08-retrieval` | Real READ handlers, hybrid retrieval, iterative scan, ADR 0013 | **sensitive** (app/db, app/harness) | yes | 7 | 4.5 |
| 9 | `m1/09-grounding` | Number guard and citation check | **sensitive** (app/harness) | no | 0 | 2.0 |
| 10 | `m1/10-loop` | The agent loop with the two READ tools, system prompt v1 | **sensitive** (app/harness, prompts) | no | 6, 9 | 4.5 |
| 11 | `m1/11-ui-answer` | UI: the ask box and the answer with its sources | **sensitive** (renders untrusted output) | no | 5 | 2.5 |
| 12 | `m1/12-ask-api` | `POST /api/ask`, rate limit, the read path in Chromium | **sensitive** (auth, harness wiring, gate path) | yes | 5, 8, 10, 11 | 3.0 |
| | | **Cut line: the end-to-end read slice is merged** | | | | **34.5** |
| 13 | `m1/13-registry` | Registry: type allowlist, depth and size caps | **sensitive** (app/harness) | no | 17 (merge order; 12 before the amendment of 2026-11-10) | 2.0 |
| 14 | `m1/14-approval` | Approval gate, `draft_email` and `create_followup` in the loop | **sensitive** (app/db, app/harness, auth) | yes | 2, 12, 13; 16, 17 (merge order, amendment) | 5.0 |
| 15 | `m1/15-proposals` | Proposal routes, the proposals panel, approval in Chromium | **sensitive** (auth, renders untrusted output) | yes | 14 | 3.5 |
| 16 | `m1/16-evals` | Recorded runs (ADR 0014), scorers, eval cases v1 | **sensitive** (runners, gate paths) | live step | 12 (amended 2026-11-10: the live step right after 12) | 5.0 |
| 17 | `m1/17-redteam` | EchoLeak and the handover attacks, unhardened rate | **sensitive** (runners, gate paths) | live step | 7, 16 | 4.5 |
| 18 | `m1/18-close` | Fresh-clone runbook, `docs/reports/m1.md`, demo script | code (docs + tests) | yes | all | 2.0 |

"DB" means the PR's own tests or steps need the shared Compose database. "Live step" means only its recording step does.

**Order.**
- **The ADRs come first** (PRs 1 and 2). PR 3 is the first infrastructure PR, so PR 4 is built beside it but merges after it.
- **The slice.** PRs 1–12 merge before anything below the cut line. PR 13 may be built during the slice but merges after PR 17 (amendment, 2026-11-10).
- **Below the cut line** (amendment, 2026-11-10): PRs 16 and 17 come first, then 13, 14, 15 and 18.
- **The database chain is serial:** 3 → 7 → 8 → 12 → 16's live step → 17 → 14 (with the `v2` re-recording) → 15 → 18. Everything else is built beside it.
- **PR 4 merges before PR 7**, because the loader chunks with the real tokenizer and embeds with the real model.
- **The loop (10) needs no database.** It is tested with a scripted model and test-local tools, and registers only the READ tools until PR 14 adds the proposal path.
- **The UI PRs (11, and the panel in 15) build against the JSON contract fixed in this plan.** PR 12 and PR 15 then prove each path in Chromium.

### Cut line

M1's end-to-end slice consists of PRs 1–12: the loader, the ONNX embedder and retrieval, then the loop with `search_client_docs` and `get_portfolio`, and an answer with sources, grounding, trace id and cost, reached through the dev login. It merges first.
- **Timur's decision:** M1 keeps its full scope and goes on below the line; M4 absorbs the time.
- **If M1 has to stop early anyway:** PRs 13–18 move to the start of M2, in the amended order 16, 17, 13, 14, 15, 18. M2's own scope is not cut (brief).
- **Price:** ordering the slice first adds about 3 hours to the critical path ("Time budget").
- **Hand check at the cut line** (Timur, with the key): Alice asks about Weber in English and German, and each answer cites Alice's documents. Bob asks about Weber and gets nothing of Alice's.

### Parallel builds: two sessions, two worktrees

**The rule.** Two PRs may be built at the same time when all of these hold:
- at most one of them needs the database;
- their dependencies are merged;
- they do not both change `pyproject.toml` and `uv.lock` (so PR 4 never runs with PR 6);
- they do not both change the same module.

| Wave | Session A | Session B | Why the pair is safe |
|---|---|---|---|
| 1 | PR 1, then PR 2 | PR 4 | no DB |
| 2 | PR 3 (DB) | PR 4 (cont.; merges after PR 3) | one DB; only PR 4 changes the lock |
| 3 | PR 7 (DB) | PR 6, then PR 5 | one DB; only PR 6 changes the lock |
| 4 | PR 8 (DB) | PR 5 (cont.), PR 9, then PR 10 | one DB |
| 5 | PR 11 | PR 10 (cont.) | no DB |
| 6 | PR 12 (DB) | PR 13 (merges after PR 17, amendment) | one DB |
| — | **cut line** | | |
| 7 | PR 16, then its live step (DB, Timur) | PR 13 (cont.; merges after PR 17) | one DB |
| 8 | PR 17 (DB: attack documents, live step) | — | |
| 9 | PR 14 (DB; the `v2` re-recording, Timur) | PR 18, text only | one DB |
| 10 | PR 15 (DB) | PR 18, text only (cont.) | one DB |
| 11 | PR 18, fresh-clone run (DB) | — | |

Waves 7–11 follow the amendment of 2026-11-10. Session B has no work in wave 8, because PR 14 needs the database that PR 17 holds. PR 13 merges at the start of wave 9, after PR 17 and before PR 14.

Never in parallel:
- any two of 3, 7, 8, 12, 14, 15, 18, or the live steps of 16 and 17;
- PR 4 with PR 6;
- PR 10 with PR 14 (both change `loop.py`);
- PR 11 with PR 15;
- PR 16 with PR 17.

### The API contract (fixed here, so the UI PRs can build ahead of their routes)

All routes need the login (401 otherwise). Every non-GET route needs the shim's Origin check (403). JSON only, with `Cache-Control: no-store`.
- **`POST /api/ask`** `{question: str (1–2000), mandate_id?: str}` → 200 with `answer`, `citations: [{record_id, title, kind}]`, `grounding: {figures, unmatched, unknown_citations}`, `proposals: [{proposal_id, tool, mandate_id, arguments}]` (empty until PR 14), `usage: {input_tokens, output_tokens}`, `cost_usd`, `stop: "end_turn"|"refusal"|"budget"|"max_turns"` and `trace_id`. Over the rate limit: 429. Without a model key or a database: 503. (PR 12)
- **`GET /api/proposals`** → `[{proposal_id, tool, mandate_id, arguments, state, created_at}]`. (PR 15)
- **`POST /api/proposals/{id}/approve`** → `{token, expires_at}`. (PR 15)
- **`POST /api/proposals/{id}/redeem`** `{token}` → `{state: "done", result_id}`; every refusal is the same 409. (PR 15)

A change to this contract is a plan amendment.

## Working protocol (every PR)

M0's protocol holds unchanged:
- **Fail-first (rule 2).** Commit 1 holds the new tests plus inert scaffolding. It is pushed alone to a **draft PR**, because CI runs on pull requests and on pushes to `main`, so a push to a feature branch alone gives no red run. The check history shows the red run; later commits make it green.
- **Scanners and catalog checks** each get a planted-violation test and a non-vacuity assertion.
- **Gate paths** make a PR sensitive and are listed in its description: `Makefile`, `pyproject.toml` `[tool.*]`, `tests/conftest.py`, `tests/ci/**`, `app/runners/**`, `evals/gate.toml`, `redteam/gate.toml` and `.github/**`.
- **Review.** The `pr-review` skill runs the reviewers of the class, each once (sensitive: spec, test and security). Then:
  - one analyzer cross-checks all findings;
  - the confirmed ones are fixed, and the affected reviewer runs once more if the skill calls for it;
  - the report goes under "## Review".

  Sensitive PRs stop for Fable's security review, and so do PRs 1 and 2 (default 8). No merge without Timur's explicit yes (rule 3).
- **Merging is Timur's, on GitHub** (Fable's addition). The agent never runs `gh pr merge`. A "yes" in the terminal is an answer to the question asked, never a merge command.
- **Hook-safe text.** Scratchpad files carry message text only (`git commit -F`, `gh pr create --body-file`), never command text.
- **ADR numbers** follow merge order: 0010 (PR 1), 0011 (PR 2), 0012 (PR 6), 0013 (PR 8), 0014 (PR 16). A PR that merges out of order takes the next free number on rebase. Amendments:
  - 0005 (PR 4);
  - 0007 (PRs 5 and 10);
  - 0008 (PRs 3, 7 and 14);
  - 0009 (PRs 3 and 5);
  - 0001 (PRs 10 and 13).
- **Definition of done** as in CLAUDE.md. The runbook and README change in the PR that changes behaviour.
- **Session report (rule 12)** in every PR description.

New for two sessions:
- **Worktrees.** Each session works in its own worktree (`git worktree add ../sitrep-m1-NN -b m1/NN-name main`), started from an up-to-date `main`. Each session's auto-memory is keyed by its path, so a PR's state lives in its PR body.
- **The database token.** Exactly one session owns the Compose database (`127.0.0.1:55432`, project `sitrep`) at a time.
  - Only the owner runs `make up`, `down`, `migrate`, `load`, `lint-test`, and `test` with `tests/db`.
  - Only the owner runs `docker-build` (the image tag `sitrep:local` is shared) and the app on port 8000.
  - The other session runs `make lint` and `uv run python -P -m pytest --ignore=tests/web --ignore=tests/db`, and leaves `tests/db` to CI.
- **Handing the token over.** The old owner runs `make down`. The new owner rebases on the latest `main` (a branch that lacks an applied migration fails `make migrate`), then runs `make up && make migrate` (`&& make load` from PR 7 on).
- **Merging.** The second PR of a pair rebases after the first merges and pushes again. Its red run stays in the check history.
- **Fable.** When a parallel pair finishes together, Timur may hand both to Fable in one sitting.
- **Untrusted text.** From PR 17 on, real attack text sits in `redteam/attacks/`. It is data (rule 9) in both sessions.

---

## PR 0 — this plan

**Files.** `docs/plans/m1-mvp-core.md`. **Tests.** None (default 16). **Evidence.** The file matches the plan Timur accepted, with his answers.

## PR 1 — ADR 0010: the loader, origin and trust

**Purpose.** Decide, before any loader code, the inputs from PR 8 sec-1, sec-8 and spec-4 and PR 5b's product sheets.

**Decision content.**
- **`origin`** is a column, with a CHECK, on every table that holds document text: `documents`, `product_sheets` (PR 7), and `drafts` and `followups` (PR 14).
  - Corpus origins: `kyc_register`, `portfolio_system`, `call_notes`, `mailbox`, `product_catalogue`.
  - Runtime origins: `agent_draft`, `agent_followup`.
  - `chunks` inherit their document's origin through the foreign key `(advisor_id, document_id)`, without repeating it.
  - `advisors`, `mandates` and `mandate_contacts` hold structured fields from the corpus (names, addresses), not document text, and carry no origin.
- **`trust`** is generated from `origin` (default 12): `mailbox` and every runtime origin are `untrusted`, the rest `internal`. Runtime-written text is always `untrusted` (Timur's answer to PR 8 sec-1). Trust is provenance, never permission: every hit is still `Untrusted` in the harness (rule 9).
- **Product sheets** get their own table, `product_sheets`:
  - no `advisor_id`; ENABLE and FORCE row-level security; a policy that lets every advisor read every row; writes for the owner only;
  - the app role holds SELECT, and the catalog test names it as the one global table;
  - no policy anywhere contains `OR advisor_id IS NULL`;
  - each sheet stores its exact URL, without a query: the input of M2's link allowlist (PR 8 sec-3).
- **Recipients per mandate** go into `mandate_contacts`, under row-level security: the input of M2's recipient allowlist (PR 8 sec-2).
- **Attack documents** carry ids with the prefix `rt-`, which the corpus never uses. The loader refuses a duplicate id across corpus and attacks, and a corpus id with the prefix. Attack documents load only on an explicit flag, into the case's target mandate, as `mailbox`.
- **The loader's identity** (question 2):
  - the migrator, after `SET ROLE` to the owner, one advisor per transaction;
  - the advisor is set through `owner_advisor_scope()` in `app/db/session.py`, which refuses any session but the migrator acting as the owner;
  - no request code can reach the helper (a scan modelled on the one for `issue()`);
  - FORCE's WITH CHECK and the composite foreign keys refuse a row whose advisor differs.
- **Loading.** The manifest is checked before loading, and replacement is idempotent. Chunks hold at most 400 tokens, with the model id and revision per vector.
- **Watch (M3, Timur's condition):** in Azure the loader gets its own identity, separate from the migrator.

**Files.** `docs/decisions/0010-loader-and-trust.md`, with a fenced `yaml` block (origins → trust, and the prefix); `tests/test_docs.py`.

**Tests that fail before the change.** `test_adr_0010_vocabulary`:
- the block exists;
- its corpus origins and trust equal `ORIGIN` and `trust_for()` in `data/synthetic/generate.py`;
- every runtime origin is `untrusted`;
- no generated id starts with the prefix.

**Review.** Spec and security reviewer, then Fable's short design read before the merge (default 8).

**Handover rows.** PR 8 sec-1, sec-8 and spec-4, and PR 5b product sheets, all decided here. **Hours** 1.0.

## PR 2 — ADR 0011: approval tokens

**Purpose.** Fix the approval-token design that ADR 0001's amendment deferred.

**Decision content.**
- **`propose()`** stores the validated call: advisor, mandate, tool, the canonical arguments (sorted-key JSON) and their sha256.
- **`approve()`** is reachable from the advisor's UI session only. It draws 32 random bytes from `secrets`, returns the token once, and stores only its sha256, with an expiry of 10 minutes.
- **`redeem(advisor, proposal_id, token)`** is one `UPDATE … RETURNING` in the advisor transaction, which requires the proposal to be approved, the hash to match, the token unexpired and not yet redeemed. It returns the stored call, validated again through the registry. The handler runs in the same transaction, so a failure rolls both back.
- **No signing key**, so no second secret (threat model, "Assets"). Every refusal looks the same. The token is never logged, traced or shown to the model.
- **Options weighed:** an HMAC-signed token (a second secret, and revocation needs state anyway); a JWT (a library, and algorithm confusion).
- **The Protocol's `redeem` signature** changes in PR 14.

**Files.** `docs/decisions/0011-approval-tokens.md`, with a `yaml` block (`token_bytes`, `hash`, `ttl_seconds`, `single_use`, `binding`); `tests/test_docs.py`.

**Tests that fail before the change.** `test_adr_0011_block`: the fields are present, `token_bytes >= 32`, `hash == "sha256"`, `single_use` is true, and the binding names the advisor, proposal, tool and arguments hash. PR 14 pins `app/harness/approval.py` to the block.

**Review.** Spec and security reviewer, then Fable's short design read before the merge (default 8).

**Handover rows.** PR 6 approval tokens, decided here. **Hours** 1.0.

## PR 3 — `SITREP_` prefix for the database URLs

**Purpose.** Timur's answer 1 to the PR 7 questions: `SITREP_DATABASE_URL` and `SITREP_MIGRATION_DATABASE_URL`, in M1's first infrastructure PR.

**Files.**
- `app/config/settings.py`: `UNPREFIXED` goes, and `env_name()` prefixes every field.
- `app/db/migrate.py`: the docstring and help text.
- `.env.example`; the runbook ("The database"); amendments to ADR 0008 (the URL names) and ADR 0009 (its Watch item closed).
- `tests/db/test_database_urls.py`, `tests/test_settings.py`.

**Tests that fail before the change.**
- `SITREP_DATABASE_URL` is read.
- An unprefixed `DATABASE_URL` naming `192.0.2.1` changes nothing: no error, and the default stands. Amended (Timur, 2026-11-09, answer 1 on #25): the entry points name such a variable on stderr (`ignored: …`), never its value; the value is still not read.
- `env_name()` is prefixed for every field.
- `.env.example` and the runbook name only the prefixed variables.

**Handover rows.** PR 7 question 1. **Timur afterwards:** rename the two lines in his own `.env`, if it sets them (the defaults are equal). **Hours** 1.5.

## PR 4 — ONNX embedder, `make model`, token chunker

**Purpose.** The real embedder of ADR 0005, with its supply-chain checks.

**Files.**
- `app/embeddings/onnx.py`: CPU only, mean pooling and L2 norm (checked against the model card while building). It checks the sha256 before every load and refuses external-data paths and custom operator domains (ADR 0005, Watch).
- `app/embeddings/chunking.py`: at most 400 tokens including the prefix, cut with the model's own tokenizer.
- `Makefile`: `model` downloads exactly `FILES` at `REVISION` into a git-ignored `models/` directory, checks the bytes and deletes a mismatch. `lint-test: up migrate model test` (default 6). `.gitignore` and `.dockerignore` change too.
- `pyproject.toml` and `uv.lock`: `onnxruntime` and `tokenizers`; the import allowlist.
- The runbook; an ADR 0005 amendment (no cache, the measured CPU speed, granite deferred).
- CLAUDE.md Commands changes are proposed in the description, and Timur applies them (rule 11): a new line for `make model`, and the `make lint-test` line reworded to "`make up`, `make migrate`, `make model`, then `make test`".
- `tests/ci/test_make_targets.py` (a gate path) gains an assertion that `lint-test` runs `model` before pytest. Nothing existing breaks there, because the file pins commands and their order, not the prerequisite list.

**Tests that fail before the change.**
- Deterministic across two subprocesses; dimension 384; unit norm; query and passage inputs differ; batch equals single within a tolerance.
- A German paraphrase lies closer than an unrelated text.
- A changed byte in any file is refused before loading. A planted graph with external data, and one with a custom domain, are refused.
- No chunk of the generated corpus exceeds 400 tokens.

**Evidence.** Passages per second and load time, on Timur's machine and in CI, pasted into the PR.

**Handover rows.** None from the table; ADR 0005's M1 items. **Hours** 3.5.

## PR 5 — Host check without the shim, plaintext and OTel capture, exception hooks

**Purpose.** Timur's answers 4 and 5 to the PR 7 questions, before any route serves data without the shim; and ADR 0007's open Watch item for M1's threads.

**Files.**
- `app/http/host_check.py`: an exact Host allowlist on every route, shim or not.
  - The default is `127.0.0.1:<SITREP_PORT>`; the image's smoke test sets its published address, and M3 sets the Azure host.
  - The shim's own check uses the same function.
- `app/observability/tracing.py` and `app/__main__.py`: `SITREP_TRACE_PLAINTEXT=1` exits 2 before binding while any variable whose name starts with `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_` is set, whatever its value.
- `app/observability/logs.py`: `threading.excepthook`, `sys.unraisablehook` and the asyncio exception handler log the exception type and the trace id only.
- Amendments to ADR 0007 and ADR 0009; the threat model ("Dev login").

**Tests that fail before the change.**
- Without the shim:
  - `Host: mallory.example` → 400 on `/`, `/health` and `/api/mandates`;
  - `127.0.0.1:<port>` → 200 on `/` and `/health`;
  - `/api/mandates` → 401, since there is no login without the shim and the route answers before it touches the database.

  None of these needs the database, so PR 5 stays "DB: no".
- The plaintext refusal for each capture variable, set and empty, run as subprocesses.
- A canary in a thread exception, an unraisable exception and an asyncio task exception reaches neither stderr nor a log record.
- `make docker-build` stays green, in CI's `docker-build` job. Locally only the database-token owner runs it.

**Handover rows.** PR 7 sec-1 and sec-5. **Hours** 2.5.

## PR 6 — Model client, ADR 0012

**Purpose.** The `ModelClient` contract of `app/harness/model.py`, and the M0 plan's "ADR for the API key workspace".

**Files.**
- `app/harness/model_client.py`: one factory, built from settings.
  - The API key is passed explicitly, and the base URL is a constant.
  - Startup is refused while any SDK variable that redirects or authenticates the client is set; the list is verified against the SDK source while building.
  - The HTTP client ignores proxy variables. No SDK retries; an explicit timeout.
  - The model comes from `SITREP_MODEL` (default 7).
  - Cost is usage times a dated price table, taken from Anthropic's pricing page at build time.
- `anthropic` in the lock, admitted by the import allowlist in this one module only (a new single-module rule).
- `docs/decisions/0012-api-key-and-model-client.md`:
  - a Console workspace for SITREP, with a spend limit Timur sets;
  - the key only in Timur's `.env`, never in CI;
  - rotation;
  - a cost estimate before every live run.
- `.env.example`; the runbook.

**Tests that fail before the change.**
- The factory refuses each redirecting variable.
- A planted canary key appears in no repr, log, span or error.
- A request through `httpx.MockTransport` carries exactly the registry's schemas, no `tool_choice`, and the configured model.
- Proxy variables pointing at a loopback listener are not used.
- Cost equals usage times the table.
- No socket opens.

**Handover rows.** None from the table. **Timur before the first live call:** the workspace, a key and a spend limit, with the key in `.env`. **Hours** 3.0.

## PR 7 — The loader

**Purpose.** The synthetic corpus in the database, as ADR 0010 decides.

**Files.**
- `db/migrations/0003_loader.sql`:
  - `documents.origin` and the generated `trust`;
  - `product_sheets`: global, FORCE, `origin` and the generated `trust`, one embedding per sheet, the exact URL;
  - `mandate_contacts`;
  - the policies and SELECT grants.
- `app/db/session.py`: `owner_advisor_scope()` sets `app.advisor_id` transaction-locally, and refuses unless `session_user` is the migrator and `current_user` is the owner.
- `app/db/load.py`, run as `python -P -m app.db.load`, and `make load`:
  - it checks the manifest, refuses any login but the migrator, and runs `SET ROLE`;
  - it replaces one advisor per transaction, then the product sheets, chunking and embedding as it goes;
  - `load_attack_documents()` applies the prefix rules (used by PR 17).
- `app/db/queries.py`: the loader's statements.
- `data/synthetic/generate.py`: a new `url` field on each product-sheet record. Today the URL exists only inside the body text (`product_url`). The golden manifest hash changes, as stated.
- Tests: `tests/db/test_rls_catalog.py` (the new tables and policy shapes), `tests/db/test_loader.py`, `tests/test_sql_hygiene.py`, and `tests/test_loader_scope.py` (the reachability scan).
- An ADR 0008 amendment; the threat model, path 2; the runbook. The CLAUDE.md line for `make load` is proposed (rule 11).

**Tests that fail before the change.**
- **The catalog:**
  - both new tables have ENABLE and FORCE, and `product_sheets` has exactly its policies;
  - no policy text contains `advisor_id IS NULL`;
  - `advisor_id` is in every key of `mandate_contacts`;
  - the grants are SELECT only.
- **The reachability scan** (Timur's condition, modelled on the `issue()` scan in `tests/test_identity.py`). An AST scan of `app/` reports two kinds of use:
  - any use of `owner_advisor_scope` (as a name, an attribute, an import or a string) outside its definition in `app/db/session.py` and its one caller in `app/db/load.py`;
  - any import of `app.db.load` anywhere in `app/`.

  The planted shapes are each reported: `from app.db.session import owner_advisor_scope`, `session.owner_advisor_scope(…)`, `import app.db.session as s; s.owner_advisor_scope`, and `from app.db import load`. Non-vacuity: the scan finds the definition and exactly one call. It reads names and literals only, as the identity scan's docstring says.
- **At runtime,** `owner_advisor_scope()` refuses the app role, and the migrator before `SET ROLE`.
- **The loader refuses** the app login and the superuser. A planted Bob document under Alice's mandate is refused by WITH CHECK or the foreign key, and nothing else is committed for that advisor.
- **Refused inputs:** a duplicate id, a corpus id with `rt-`, and a manifest mismatch.
- **Trust:** an INSERT that names `trust` fails; every runtime origin reads `untrusted`.
- **Product sheets:** the app reads every one and writes none.
- **Vectors:** every vector carries the model id and revision.
- **Idempotence:** two loads give the same counts: 40 mandates, 344 documents, 6 sheets.

**Handover rows.** PR 8 sec-8 and spec-4; PR 5b product sheets; PR 8 sec-2 and sec-3 (the data; question 1). **Hours** 4.5.

## PR 8 — Retrieval and the READ handlers

**Purpose.** `search_client_docs` and `get_portfolio` for real, scoped by row-level security.

**Files.**
- `app/harness/handlers.py`: a map from tool name to handler, kept apart from the specs.
  - Each handler runs in its own advisor transaction and returns `Untrusted` with `Provenance(tool, record_ids)`; nothing is kept past the block.
  - Search fuses full-text search per language with a vector search by reciprocal rank, and includes the product sheets. `DocumentHit` gains `kind` and an optional `mandate_id`.
  - A mandate id is looked up inside the transaction, so another advisor's mandate is "no such mandate", the same answer as a made-up id.
- `app/db/session.py`:
  - `hnsw.iterative_scan` is set transaction-locally (default 13);
  - the connection the block yields becomes an invalidated handle on exit (PR 5b sec-5).
- `db/migrations/0004_search_indexes.sql`: GIN indexes on both `tsv` columns.
- `docs/decisions/0013-retrieval.md`: the ranking, the iterative-scan mode, the limits.
- `tests/db/test_forged_tool_argument.py`, retargeted to the real handler.

**Tests that fail before the change.**
- **The boundary.** Alice's searches never return Bob's or Carol's record ids, parametrised over EN and DE queries, including Weber's family name searched as Bob.
- **A foreign mandate.** `mandate_id` of Bob's mandate, sent as Alice, gives an empty result for both tools.
- **Forged payloads.** PR 5b's payloads pass through the real handler and return only Alice's rows.
- **Iterative scan.** With row-level security hiding most chunks, a vector query still returns `limit` hits; the test is red without the iterative scan.
- **Clean connections.** After a search, the next checkout passes the pool guard, and the handle raises on use after the block.
- **Product sheets** reach all three advisors.
- **Language.** The German stem matches; the English branch does not match a German document.
- **The index.** EXPLAIN shows the GIN index once the planner may not take a sequential scan.

**Handover rows.** PR 5b sec-5, hnsw.iterative_scan and GIN index; PR 6 test-14 (the READ tools) and forged-argument handler. **Hours** 4.5.

## PR 9 — Grounding: number guard and citation check

**Purpose.** Threat-model path 7, and the one named accessor that `context.py` promised.

**Files.**
- `app/harness/grounding.py`: it finds figures in DE and EN formats (amounts, percentages, dates) and checks each against the sources of this request, and each cited record id against the ids seen. The result holds counts and ids, never figures.
- `app/harness/context.py`: `source_text()`, whose only caller is `grounding.py` (a scan pins it).
- Amended (Timur, 2026-11-10): an ADR 0007 amendment for the report's shape (counts and the cited ids a tool returned).
- Handover (the agent's derivation from PR 9's review, not Timur's decision): PR 10 amends ADR 0007 for the trace keys, which needs room first (the ADR is at its 600 words); `prompt.py` takes the citation form from `grounding.CITATION_FORM`; the report carries record ids only, so title and kind for the API's `citations` come from PR 8's hits or a lookup in PR 12, and the API's `grounding` object is the report's three counts.

**Tests that fail before the change.**
- A figure that appears in the sources passes; a figure that appears nowhere is counted; `1.250.000,00 EUR` matches `1,250,000.00`.
- A citation of an unseen id is counted.
- A figure that appears only in an attacker's e-mail still counts as grounded: the guard proves provenance, not truth (stated in the docstring).
- The accessor's callers are exactly one file.

**Handover rows.** None from the table. **Hours** 2.0.

## PR 10 — The agent loop with the two READ tools

**Purpose.** ADR 0001's own loop, with every control in it. PR 14 adds the proposal path.

**Files.**
- `app/harness/loop.py`: `run(advisor, question, mandate_id)` returns the answer, citations, grounding, usage, cost, stop and trace id.
  - Every `stop_reason` is handled, `refusal` and `model_context_window_exceeded` included. `tool_result` blocks come first, and tools may be called in parallel.
  - `max_turns`, and a `TokenBudget` charged after every call and every `wrap()`. A limit ends the turn with a stated reason and never truncates silently.
  - READ results go through `wrap()`. A `RejectedCall` returns error types and locations to the model.
  - The registry the loop uses holds `search_client_docs` and `get_portfolio` only.
- `app/harness/prompt.py`: `PROMPT_VERSION = "v1"`, built from branding. The prompt asks for cited record ids and gives no investment advice; nothing depends on it staying secret.
- `app/observability/tracing.py`: the attributes approved by name: prompt sha256 and version, model, token counts, stop, turns, tool names, error types and grounding counts.
- Amendments to ADR 0007 and ADR 0001.

**Tests that fail before the change** (a scripted model client and test-local tools):
- each stop reason, the ordering rule, and the budget and turn limits;
- no `tool_choice` in any request, and the tools equal `registry.schema()`;
- every tool result in a request sits inside `<untrusted`;
- only the two READ tools are offered;
- a canary in the question, in a tool result, in the answer and in a refused argument key reaches no span, event or log; the key name is in the model's `tool_result` and never in a span.

**Handover rows.** PR 6 error locations. **Hours** 4.5.

## PR 11 — The UI: ask, and the answer with its sources

**Purpose.** The brief's chat UI, served by the app and safe with hostile model output. The proposals panel comes in PR 15.

**Files.**
- `app/web/static/index.html`, `app.js` and `app.css`: no inline script or style, under the M0 CSP.
- The answer, citations, trace id, cost and grounding result are rendered with `textContent` only.
- In M1, links appear as text, and no image is ever created from model output.

**Tests that fail before the change** (Playwright, with `/api/ask` answered by route interception):
- An answer carrying `<img src=…{domain.canary}…>` and a reference-style canary link:
  - shows the text literally;
  - creates no `img` or `a` element;
  - sends no request off the origin;
  - raises no `securitypolicyviolation`.
- Citations show their titles.
- The page title comes from branding.

**Handover rows.** None from the table. **Hours** 2.5.

## PR 12 — `POST /api/ask` and the read path in Chromium

**Purpose.** The slice, wired: dev login → loop → READ handlers → an answer with sources.

**Files.**
- `app/api/ask.py`: a sync route.
  - Each tool call opens and closes its own transaction, so no connection is held during a model call.
  - The rate limit is per verified advisor, in process.
  - The model client is built lazily; without a key, the route answers 503.
- `create_app(model_client=…)` for tests.
- `tests/web/`: the read path in Chromium, with a scripted model. Log in as Alice, ask, and see a cited answer whose sources are Alice's documents. The `web-test` target gains `up migrate model load` (a gate path). The reworded CLAUDE.md `make web-test` line is proposed in the description, and Timur applies it (rule 11).

**Tests that fail before the change.**
- 401 without a login; 403 without the Origin.
- Bob asking about Weber through the API gets no record id of Alice's.
- Over the limit, Alice gets 429 while Bob is not limited.
- During the scripted model call, the pool has all its connections free.
- No text in spans.
- 503 without a key, and `/health` unaffected.

**Handover rows.** PR 5b sec-5 (no connection held, no streaming: default 10); PR 7 cross-advisor (the API level). **Evidence.** Timur's hand check at the cut line. **Hours** 3.0.

## Cut line — the end-to-end read slice is merged

## PR 13 — Registry: type allowlist, depth and size caps

**Purpose.** PR 6 sec-2 and sec-11, before tools with free-text recipients and bodies reach the loop.

**Files.** `app/harness/tools.py`, `tests/harness/test_registry.py`, and an ADR 0001 amendment (one line).
- Argument annotations come from a reviewed set: `str`, `int`, `bool`, `datetime.date`, `Literal`, tuples of those, and nested frozen `Arguments` models, with `Field` constraints only.
- Refused: every custom validator (before, after, wrap and plain; field and model), `WithJsonSchema`, `SkipJsonSchema`, `json_schema_extra` and `RootModel`.
- `validate_call()` refuses raw arguments nested deeper than 4, or larger than 32 KiB as JSON (`too_deep`, `too_large`), before pydantic sees them.

**Tests that fail before the change.**
- Each refused feature, planted (they register today, as measured in PR 6).
- The four MVP tools still register.
- A 5-deep argument and a 33 KiB argument each give a structured error, and no handler runs.

**Handover rows.** PR 6 sec-2 and sec-11. **Hours** 2.0.

## PR 14 — The approval gate and the two proposal tools

**Purpose.** ADR 0011 built, with the first writes the app role may do, and `draft_email` and `create_followup` in the loop.

**Files.**
- `db/migrations/0005_approvals.sql`:
  - `proposals`, `drafts` and `followups`, with advisor-scoped keys, foreign keys to `mandates`, ENABLE and FORCE, and the origins `agent_draft` and `agent_followup`;
  - grants: INSERT and SELECT, and UPDATE on the proposal's state columns only.
- `app/harness/approval.py`: the gate, with `redeem(advisor, proposal_id, token)` returning the stored call.
- `app/harness/handlers.py`: `draft_email` stores a draft (nothing is sent; there is no mail system), and `create_followup` stores a follow-up.
- `app/harness/loop.py`: DRAFT and SIDE_EFFECT calls go to `gate.propose()` and return a `Proposal`. The prompt tells the advisor that approval happens in the UI (`PROMPT_VERSION` "v2").
- `tests/db/test_rls_catalog.py`: a grant matrix per table.
- An ADR 0008 amendment; the threat model, path 4.
- **From the amendment of 2026-11-10:**
  - the cases that need a proposal tool (the eval draft and follow-up; EchoLeak's `draft_email` form; the sec-2 draft);
  - Timur's `v2` re-recording of both suites, after the agent's cost estimate;
  - `evals/results/v2.md`, and the `v2` rate added to `redteam/results/m1.md`.

**Tests that fail before the change** (the contract tests):
- **The happy path.** Propose stores; approve returns a token once, and only its hash is in the database; redeem runs the stored call once.
- **Refused alike:** a second redeem, an expired token, a wrong token, another advisor's proposal (not found under row-level security), and arguments a superuser changed after approval (the hash binding).
- **No tool and no loop path reaches `approve()`** (a scan). A DRAFT call runs no handler (a spy).
- **A foreign mandate.** A `draft_email` or `create_followup` that names Bob's mandate, sent as Alice, stores nothing.
- **Runtime rows.** Drafts and follow-ups read `untrusted`.
- **Traces** carry proposal ids only.

**Handover rows.** PR 6 approval tokens (built here) and test-14 (the DRAFT and SIDE_EFFECT tools); PR 8 sec-1 (built here). **Hours** 5.0.

## PR 15 — Proposal routes, the proposals panel, approval in Chromium

**Purpose.** The rest of the contract: the advisor sees what will run, and approves it.

**Files.**
- `app/api/proposals.py`: the three routes, under the login and the Origin check.
- The UI's proposals panel shows the stored arguments, every recipient included, as text. Approve calls `approve`, then `redeem`.
- `tests/web/`: approval in Chromium. Ask for a draft, see it as a proposal with all its recipients, approve it, and see the draft stored. A second redeem is refused.

**Tests that fail before the change.**
- 401 and 403, as in PR 12.
- Every refusal is the same 409.
- A proposal with three recipients shows all three.
- The token never appears in `/api/ask` output or in a span.

**Handover rows.** None beyond PR 14's. **Hours** 3.5.

## PR 16 — Recorded runs and evals v1

**Purpose.** The eval harness scores, and the first cases are recorded.

**Files.**
- `docs/decisions/0014-recorded-runs.md` (question 3):
  - the transcript format;
  - the stale check (the prompt, tool-schema and corpus-manifest hashes);
  - the live protocol: an in-process dev login as the case's advisor, through the app's own routes, so there is no new identity path; local only, and the shim is not in the image.
- `app/runners/`:
  - `--estimate` (cost from cases × samples × the measured tokens, with no network) and `--live --yes`;
  - the scorers: valid citations, grounding, expected tools, no foreign record id, and the refusal marker;
  - `gate.toml` keys;
  - `evals/results/<prompt version>.md` (the eval-run skill's naming): `v1.md` here, `v2.md` from PR 14 (amendment, 2026-11-10).
- `evals/cases/`, about 14 cases in EN and DE in all, fewer at `v1` (the draft and follow-up ones come with PR 14, amendment):
  - the Weber briefing;
  - portfolio figures;
  - a product sheet question;
  - a draft e-mail and a follow-up (proposals): **these land with PR 14** (amendment);
  - the pushy "guaranteed return" (a refusal);
  - Bob asking about Weber (no Weber record).
- `evals/recorded/<prompt version>/`, recorded by Timur (default 17) after PR 12, at `v1` (amendment, 2026-11-10).

**Tests that fail before the change.**
- Planted transcripts pass and fail each scorer.
- A stale hash fails, a missing recording fails, and a credential header fails (as in M0).
- `--live` without a key exits 2; `--estimate` opens no socket.
- The positive path, with the first recorded case, exits 0 with a scored line.

**Handover rows.** PR 7 cross-advisor cases (the eval half). **Hours** 5.0, plus Timur's live run.

## PR 17 — Red team v1, unhardened

**Purpose.** The threat model's first case and the handover attacks, measured before M2.

**Files.**
- `redteam/attacks/`, in EN and DE, through placeholders:
  - the EchoLeak e-mail in Weber's mailbox, in six forms: an inline link, a reference-style link, an image, a `draft_email` argument (body and recipient), split across two e-mails, and a link on the staff domain (sec-3); the `draft_email` form lands with PR 14 (amendment, 2026-11-10);
  - a draft to an address outside the mandate (sec-2), which also lands with PR 14;
  - a document that closes `</untrusted>`;
  - a forged identity key (`berater_id`, `benutzer`);
  - an e-mail that asks for another advisor's mandate.
- Attack documents are loaded with the `rt-` prefix (PR 7).
- **The scorer.** Success is the canary in the answer or in any tool argument, proposals included. For the cross-advisor and identity families, success is a foreign record id.
- **The rate.** The attack success rate per family and language, over N samples per variant, with N set by the estimate. `redteam/results/m1.md` is the unhardened baseline.
- **Stop criterion.** The PR 8 detectors are not used as `SCOPE.md`'s stop criterion (sec-6); the runner's label-based host rule and the credential rule are.

**Tests that fail before the change.**
- The canary in a tool argument is detected, in EN and DE, on planted transcripts.
- A foreign record id counts as success.
- A planted attack with a literal domain is refused (as in M0).
- The gate ratchet.

**Timur before this PR:** decide the Bash-side guard for the guardrail files (the M0 report, item 6), because this PR brings the first real attack text into the repository.

**Handover rows.** PR 6 red-team cases; PR 7 cross-advisor cases (the red-team half); PR 8 sec-2 and sec-3 (the cases); PR 8 sec-6. **Hours** 4.5, plus Timur's live run.

## PR 18 — M1 close

**Files.**
- `docs/runbooks/local-setup.md`: the fresh-clone path with `model`, `data` and `load`, and the demo script.
- `docs/reports/m1.md`: measured facts, the M2 handovers, rule 12.
- `docs/threat-model.md`: M1 status for paths 1, 2, 4, 7 and 8.
- `README.md`: status.
- `tests/test_docs.py`.

**Tests that fail before the change.** The fresh-clone block equals the verification sequence, and the M1 report has its sections and handover ids. **Hours** 2.0.

## Handover coverage (every row of `docs/reports/m0.md`)

| Id | Lands in | Note |
|---|---|---|
| PR 5b sec-5 | PR 8 (the invalidated handle), PR 12 (no connection held, no streaming) | |
| PR 5b hnsw.iterative_scan | PR 8 | transaction-locally (default 13) |
| PR 5b product sheets | PR 1 (ADR), PR 7 | |
| PR 5b GIN index | PR 8 | with EXPLAIN evidence |
| PR 6 sec-2 | PR 13 | type allowlist (default 11); below the cut line |
| PR 6 sec-11 | PR 13 | below the cut line |
| PR 6 test-14 | PR 8 (READ), PR 14 (DRAFT, SIDE_EFFECT) | |
| PR 6 forged-argument handler | PR 8 | |
| PR 6 approval tokens | PR 2 (ADR), PR 14 | |
| PR 6 error locations | PR 10 | |
| PR 6 red-team cases | PR 17 | |
| PR 7 sec-1 | PR 5 | |
| PR 7 sec-5 | PR 5 | ADR 0007 amended |
| PR 7 question 1 | PR 3 | |
| PR 7 cross-advisor cases | PR 12 (API test), PR 16 (eval), PR 17 (red team) | |
| PR 8 sec-1 | PR 1 (ADR), PR 14 | |
| PR 8 sec-2 | PR 7 (data), PR 14 (the draft case, amendment of 2026-11-10); **enforcement deferred to M2** | brief: M1 measures unhardened (question 1) |
| PR 8 sec-3 | PR 7 (exact URLs), PR 17 (variant); **enforcement deferred to M2** | as sec-2 |
| PR 8 sec-4 | — | fixed in M0; the race on local, git-ignored output stays; nothing for M1 |
| PR 8 sec-5 | — | fixed in M0 |
| PR 8 sec-6 | PR 17 | the detectors are not the stop criterion |
| PR 8 sec-7 | — | fixed in M0 |
| PR 8 sec-8 | PR 1 (ADR), PR 7 | |
| PR 8 spec-4 | PR 1 (ADR), PR 7 | |

The M0 plan's M1 list lands as well:
- the loop, the handlers and retrieval (PRs 8, 10 and 14);
- the loader, chunking, the ONNX embedder and `make model` (PRs 4 and 7);
- the positive-path runner test (PR 16);
- the canary-in-tool-argument test (PR 17);
- the UI (PRs 11 and 15);
- the API-key ADR (PR 6).

ADR 0007's Watch items (the exception hooks and the approved prompt attributes) land in PRs 5 and 10.

## What Timur does (outside the agent)

**Before the first live call** (PR 6 on, at the latest the cut-line hand check after PR 12): create the SITREP workspace in the Anthropic Console, a key in it and a spend limit, and put the key in `.env` himself.

**During M1:**
1. Start the second session in its worktree, and be present for prompts in both.
2. Get Fable's security review for PRs 3–17 (15 PRs, 10 of them before the cut line), plus Fable's short design read of ADR 0010 and ADR 0011 (PRs 1 and 2).
3. Merge every PR himself on GitHub, after the review report and, where required, Fable's.
4. Run the cut-line hand check after PR 12, and the live recordings himself (`! uv run python -P -m app.runners.evals --live --yes`, then the red team's), each after the agent's estimate. Since the amendment of 2026-11-10 there are three: PR 16's and PR 17's at `v1` after PR 12, then both suites at `v2` in PR 14.
5. After PR 3, rename his own `.env` lines if he has any.
6. Decide the Bash-side guard before PR 17. Since the amendment, that is about 24.5 hours in, after PR 16.
7. Apply the CLAUDE.md Commands changes (rule 11): new lines for `make model` (PR 4) and `make load` (PR 7); the reworded `make lint-test` (PR 4) and `make web-test` (PR 12) lines; and M0's two lines.
8. Watch `lint-test`'s time from PR 4 on: past three minutes, the verified cache becomes the fallback (default 6).

**Still open from M0:** the ruleset, Dependabot #7, #16 and #17, and the `AZURE_CONFIG_DIR` folder. The merge checklist is dropped (default 18).

## Time budget

| PR | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Build h | 1.0 | 1.0 | 1.0 | 1.5 | 3.5 | 2.5 | 3.0 | 4.5 | 4.5 | 2.0 | 4.5 | 2.5 | 3.0 | 2.0 | 5.0 | 3.5 | 5.0 | 4.5 | 2.0 |

- **Total:** about 56.5 build hours, 34.5 of them for the slice (PRs 0–12). Each estimate includes the session time for tests first, the CI red run, review and fixes. Not included: Fable's own time, Timur's merge decisions and the live runs (three since the amendment of 2026-11-10).
- **Critical path with two sessions** (the waves above):
  - **The slice merges after about 18.5 hours,** about 10 days at 2 hours a day. Session B carries it:
    - PR 4: 0–3.5 h;
    - PR 6: to 6.5 h;
    - PR 5: to 9.0 h;
    - PR 9: to 11.0 h;
    - PR 10: to 15.5 h.

    Meanwhile session A runs PRs 1, 2 and 3 (0–3.5 h), PR 7 (to 8.0 h), PR 8 (to 12.5 h) and PR 11 (to 15.0 h). PR 12 waits for PR 10 and runs from 15.5 to 18.5 h.
  - **M1 closes after about 40.5 hours,** about 20 days (amendment, 2026-11-10). Session A carries it after the slice:
    - PR 16 and its live step: to 24.5 h (5.0 + about 1.0);
    - PR 17: to 29.0 h. This is where the unhardened EchoLeak rate is measured;
    - PR 14 with the `v2` re-recording: to 35.0 h (5.0 + about 1.0);
    - PR 15: to 38.5 h;
    - PR 18: to 40.5 h.

    Session B builds PR 13 beside PR 16, then PR 18's text beside PRs 14 and 15, all off the critical path.
  - **Against the accepted order** (34.5 h to the close, with the EchoLeak rate at about 32.5 h): the rate arrives about 3.5 h earlier, and the close comes about 6 h later. PR 16's offline build (5.0) moves onto the critical path, because PR 16 depends on PR 12. The `v2` re-recording adds about 1.0.
  - **Ordering the slice first costs about 3 hours** on the critical path against the draft's unordered sequence (about 31.5 hours; question 4 rounded it to 31).
- **Against M0 and the brief:** M0 took 12 days against a plan of 3–4, so these are session hours, not calendar days, and Fable stops add waiting time.
- **Re-baselined (question 4).** The brief's days 4–9 become about days 4–24 (days 4–21 before the amendment of 2026-11-10), with the cut-line demo at about day 14. M4 absorbs the slip.

## Left out of M1 (stated, not hidden)

- **M2:**
  - the output policy: exact-URL links and per-mandate recipients enforced (question 1), plus the canary scan and alarm;
  - provenance hardening: a delimiter nonce and Unicode tag, bidi and zero-width characters (PR 6 sec-9), the `_value` read-scan, pickle and slots (sec-7), a record-id pattern (sec-8), and CR and LF in recipients;
  - the CSP-off rate, and the regression gate against a baseline (the brief's "eval gate in CI");
  - 15–20 automated variants, and the Haiku guard layer;
  - granite-r2 against e5 (default 15);
  - if M1 stops at the cut line: PRs 13–18 first (question 4).
- **M3:**
  - Azure and the Entra login;
  - **the loader's own identity, separate from the migrator** (Timur's condition, ADR 0010);
  - the model file in or beside the image: the image carries no model in M1, so search fails there;
  - `log_parameter_max_length`.
- **M4:** the poisoning path and origin filters (`trust` is ready for them), the Foundry switch, the cost dashboard.
- **Later:**
  - the signed advisor context;
  - the guard's rule names in logs, and `psycopg.pool` at WARNING (PR 5b sec-7);
  - session expiry and a store cap (kept out in M0);
  - one database per worktree, which would lift the serial database chain but changes `compose.yaml` and the port.
- **Not for M1, from PR 8:** the street shapes the detector misses, and a mutation control for the ownership checks.

## Verification at the end of M1

- **Fresh clone:** `make up && make migrate && make model && make data && make load && make test && make evals && make redteam && make docker-build && make web-test` all pass, with no API key.
- **CI:** six green checks on `main`; `evals` and `redteam` score recorded runs, with `min_cases` equal to the committed cases.
- **By hand (Timur, with the key):**
  - Alice asks about Weber in English and German and gets cited answers with a trace id, cost and grounding;
  - a draft exists only after Approve, and a second redeem is refused;
  - Bob asking about Weber gets nothing of Alice's;
  - `redteam/results/m1.md` states the unhardened success rate per family.

## What is shaky

- **Time.** About 40.5 critical-path hours (34.5 before the amendment of 2026-11-10) against a 12-hour budget, 15 Fable stops, and 2 short design reads. The serial database chain keeps half the PRs from running in parallel.
- **Transcripts do not re-run the harness.** The stale check is only as good as the hashes it covers: a handler change that leaves the prompt and schemas alone is not caught by the gates, only by the tests.
- **The unhardened rate is a small sample**, N per variant on one model version, so its intervals are wide.
- **The loader writes as the owner**, with the migrator's login: local only. It adds a second `set_config` call site, guarded by a role check and a reachability scan that reads names, not `getattr()` or strings built at runtime.
- **The live runner's in-process login** builds its own ASGI scope, so the entry point's socket checks do not apply to it. It runs locally only, and the shim is not in the image.
- **Grounding** extracts figures with regular expressions, misses figures written as words, and proves provenance, not truth.
- **Refusal correctness** is scored by a deterministic marker: a weak check.
- **CI downloads about 490 MB** from Hugging Face on every `lint-test` and `web-test` run: hash-checked, but a network dependency. Past three minutes for `lint-test`, the fallback is a cache verified by sha256 after every restore (default 6).
- **SDK behaviour** (environment variables, retries, proxies) is stated from memory here; PR 6 verifies it against the source.
- **Real attack text enters the repository in PR 17**, while the Bash-side guard for the guardrail files is still Timur's open decision.
- **Rebase churn** in the runbook, the threat model and the README for every second PR of a pair.
