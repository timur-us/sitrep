# ADR 0014: Recorded runs and the offline eval gate

## Status

Proposed (M1 PR 16; question 3 of the M1 plan, answered by Timur on 2026-11-08: score transcripts).

## Date

2026-11-13

## Context

SITREP (situation report) answers with a model, so its behaviour is measured on samples. CI holds no key (ADR 0012) and no database for the `evals` job, and a live run costs money, so it runs by hand after an estimate (plan default 17). The gate must be deterministic, offline, and notice when a recording no longer describes the code.

## Options

- **Live runs in CI:** a key in CI, against ADR 0012, and a gate that flickers.
- **Replaying model replies through the harness:** the handlers, retrieval and a loaded database in the `evals` job; the closest check, at the price of CI's whole setup.
- **Scoring transcripts:** cheap and deterministic; a handler change that leaves prompt, tools and corpus alone goes unseen.

## Decision

- **Transcripts.** One file per case, `evals/recorded/<prompt version>/<case id>.json` (`app/runners/transcript.py`): the stale keys, then per sample the date, the system prompt as sent, the last request's messages, every reply, and the route's status and body.
- **Stale check.** A recording names the prompt version and the template's sha256, the sha256 of the tools as sent (canonical JSON), the reviewed corpus's manifest sha256 and the model. Any difference fails the gate; so does a system prompt that is not the current template filled in with the sample's date, and a body whose grounding counts or cited ids differ from what the transcript gives.
- **Scoring.** The gate scores the current prompt version's directory only; older ones stay as evidence. Each case's samples pass or fail its scorers (`app/runners/scoring.py`): answered, citations, grounding (`grounding.check()` over the recorded tool results, which `wrap()`'s form gives back exactly; planted figures may only appear unmatched), tools, refusal and gap markers per language, and **no_foreign_record**, hard, from the generator's ownership, never the database's.
- **Gate.** `evals/gate.toml`: `min_cases` (the committed count), `samples` per case and `min_pass_rate`, pre-registered at 0.8 before the first recording. A hard failure fails at any rate.
- **Live protocol.** `--live --yes`, by Timur only: the key from `.env` as `python -m app` reads it, the default model only, one date per run. Every sample asks through a fresh app's own routes, the dev login and `POST /api/ask`, over ASGI from 127.0.0.1: no new identity path. The image carries no dev login.
- **Estimate.** `--estimate`, offline: asks times the measured mean usage of current recordings, else the assumed tokens below, at the US-only rate; with the ceiling of every ask spending its whole budget.

```yaml
format: 1
stale_keys: [prompt_version, prompt_sha256, tools_sha256, corpus_sha256, model]
scorers: [answered, citations, grounding, tools, no_foreign_record, refusal, gap]
hard: [no_foreign_record]
samples: 3
min_pass_rate: 0.8
assumed_tokens: {input_tokens: 12000, output_tokens: 1500}
```

## Consequences

- **Easier:** the gate runs in seconds with no key or database; a changed prompt, tool or corpus fails it until it is recorded again; failures name the scorer and the sample, never text.
- **Harder:**
  - recordings put prompts, tool results and answers in the repository: synthetic only (rule 7);
  - every prompt version means a live run, and a dated prompt ties a past-date case to the recording's date;
  - the markers are fixed phrases, a weak check, and three samples a small one;
  - the in-process login skips the entry point's socket checks.
- **Watch:** handler changes the hashes miss (the plan's "What is shaky"); recordings' size; M2's regression gate against this baseline.

## Sources

- `docs/plans/m1-mvp-core.md`, question 3, defaults 14 and 17, PR 16 and the amendment of 2026-11-10 (2026-11-08).
- ADR 0012, the key only in `.env` and the estimate before every live run (2026-11-09).
- `.claude/skills/eval-run/SKILL.md`, pre-registration and the results file (2026-10-27).
- The token counts behind the assumed tokens, measured on the loaded corpus with the loop's estimate, in PR #38's description (2026-11-13).
