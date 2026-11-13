# ADR 0014: Recorded runs and the offline eval gate

## Status

Proposed (M1 PR 16; question 3 of the M1 plan, answered by Timur on 2026-11-08: score transcripts).

## Date

2026-11-13

## Context

SITREP (situation report) answers with a model, so its behaviour is measured on samples. CI holds no key (ADR 0012) and no database for the `evals` job, and a live run costs money, so it runs by hand after an estimate (plan default 17). The gate must be deterministic, offline, and notice when a recording no longer describes the code.

## Options

- **Live runs in CI:** a key in CI, against ADR 0012, and a gate that flickers.
- **Replaying model replies through the harness:** retrieval and a loaded database in the `evals` job; the closest check, at the price of CI's whole setup.
- **Scoring transcripts:** cheap and deterministic; a handler change that leaves prompt, tools and corpus alone goes unseen.

## Decision

- **Transcripts** (`app/runners/transcript.py`): one file per case and prompt version, naming the advisor, then per sample the date, the system prompt, the last request's messages (sent or failed), every reply, the route's status and body, and the loop's error word.
- **Stale and bound.** The prompt version and template sha256, the tools' sha256, the corpus manifest's sha256 and the model must be current; the system prompt must be the template on the sample's date; the body's grounding and cited ids must be what the transcript gives; the advisor and each first message must be the case's. The expectations are not bound: a reviewed change scores the same evidence again.
- **Scoring** covers the current version only; older recordings stay as evidence. Scorers (`app/runners/scoring.py`): answered, citations, grounding (`grounding.check()` over the tool results as `wrap()` rendered them; planted figures may only appear unmatched), tools, refusal and gap markers per language, and **no_foreign_record**, hard: another advisor's record id per the generator, not the database, or the loop's `guard` error.
- **Gate:** `min_cases`, `samples` and `min_pass_rate`, 0.8, pre-registered. A hard failure fails at any rate.
- **Live protocol.** `--live --yes`, by Timur: the key as `python -m app` reads it, the default model, one date per run, no abbreviated flags. Each sample asks a fresh app through the dev login (ADR 0009) and `POST /api/ask`, over ASGI from 127.0.0.1: no new identity path. A 401, 403 or 503, an unpriced reply, or spending past the estimate's ceiling stops the run.
- **Estimate**, offline: the mean usage of current, valid recordings, else 12,000 input and 1,500 output tokens per ask (measured inputs: prompt and tools about 1,000, a search result 1,300 to 1,600, a portfolio 250), at the US-only rate.
- **The date** comes from the route (`today`); the loop reads none.

```yaml
format: 1
stale_keys: [prompt_version, prompt_sha256, tools_sha256, corpus_sha256, model]
bound: [advisor, question]
scorers: [answered, citations, grounding, tools, no_foreign_record, refusal, gap]
hard: [no_foreign_record]
samples: 3
min_pass_rate: 0.8
assumed_tokens: {input_tokens: 12000, output_tokens: 1500}
```

## Consequences

- **Easier:** seconds, no key or database; a changed prompt, tool, corpus or question fails until recorded again; failures name scorer and sample, never text.
- **Harder:** recordings put prompts, tool results and answers in the repository (synthetic only); every prompt version needs a live run; markers are fixed phrases; three samples are few; the corpus hash is asserted, not read from the database; the in-process login skips the entry point's socket checks.
- **Watch:** handler changes the hashes miss; M2's regression gate against this baseline.

## Sources

- `docs/plans/m1-mvp-core.md`, question 3, defaults 14 and 17, PR 16, and the amendments of 2026-11-10 and 2026-11-13 (2026-11-08).
- ADR 0012, the key only in `.env` and the estimate before every live run (2026-11-09).
- `.claude/skills/eval-run/SKILL.md`, pre-registration and the results file (2026-10-27).
