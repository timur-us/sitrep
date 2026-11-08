# ADR 0010: The loader, origin and trust, product sheets, attack ids

## Status

Accepted (Timur, 2026-11-08: the M1 plan, PR 1, and question 2); this text awaits Fable's design read.

## Date

2026-11-08

## Context

SITREP (situation report) loads its synthetic corpus in M1 (PR 7). M0's handovers set its shape (PR 8 sec-1, sec-2, sec-3, sec-8, spec-4; PR 5b product sheets). The app role only reads, so the loader writes as the owner (question 2).

## Options

- **Trust written by the loader:** one bad write makes e-mail `internal`.
- **A trust table:** rows the owner can update.
- **Product sheets in `documents`** under `OR advisor_id IS NULL`: the bypass sec-8 forbids. **Copied per advisor:** three copies to keep equal.
- **Attack documents in a table of their own:** a path production never reads.

## Decision

- **`origin`** is `text NOT NULL` with a CHECK, on every table holding document text (the block's `tables`). The loader derives it from the record's type through the generator's `ORIGIN`, refusing an unknown type or a disagreeing `origin` or `trust`. `chunks` inherit it through `(advisor_id, document_id)`; a hit without one counts as `untrusted`. `advisors`, `mandates` and `mandate_contacts` carry none.
- **`trust`** is a STORED generated column (default 12): `internal` for the block's internal origins, else `untrusted`: a new origin fails closed, and no writer sets it. Trust is provenance, never permission: every hit stays `Untrusted`; `internal` relaxes no allowlist, scan or guard (rule 9).
- **Runtime text** (`drafts`, `followups`) is `untrusted`, unretrieved in M1 (default 5).
- **`product_sheets`** is the one global table, the catalog test's sole exemption: no `advisor_id`; ENABLE and FORCE; SELECT for the app role under `NULLIF(current_setting('app.advisor_id', true), '') IS NOT NULL`, so a reused connection reads nothing; a write policy for the owner alone, since FORCE binds it. No policy admits `advisor_id IS NULL`. Each sheet stores its exact `https` URL (no query, fragment or user part) for M2's link allowlist. A sheet over the chunk cap is refused, never cut.
- **`mandate_contacts`** holds each mandate's recipients under row-level security, from the mandate record's `client_email`, never from e-mail headers, for M2's recipient allowlist.
- **Attack documents** have ids starting `rt-`, compared case-insensitively. Corpus ids with it, attack ids without it, and duplicates are refused. Behind an explicit flag it writes only `documents` and `chunks`: into the case's target mandate, which must exist in the corpus, under its corpus advisor, as `mailbox` whatever the case says.
- **The loader's identity:** the migrator after `SET ROLE` to the owner, one advisor per transaction. `owner_advisor_scope()` in `app/db/session.py` sets the advisor and refuses any session but the migrator as the owner. An AST scan keeps request code from it and from `migration_database_url`. WITH CHECK and the composite foreign keys refuse another advisor's row.
- **Loading:** every sha256 is checked against the manifest first. Each advisor's corpus rows are replaced by DELETE in that scope, never TRUNCATE (which ignores row-level security); no runtime row cascades from them, and a load without the flag leaves no attack row. Chunks hold at most 400 tokens, with model id and revision per vector.

```yaml
corpus_origins:  # origin: trust, as data/synthetic/generate.py assigns them
  kyc_register: internal
  portfolio_system: internal
  call_notes: internal
  mailbox: untrusted
  product_catalogue: internal
runtime_origins:  # written by the app (PR 14)
  agent_draft: untrusted
  agent_followup: untrusted
tables:  # the origins each table's CHECK allows
  documents: [kyc_register, portfolio_system, call_notes, mailbox]
  product_sheets: [product_catalogue]
  drafts: [agent_draft]
  followups: [agent_followup]
attack_id_prefix: "rt-"
attack_origin: mailbox
```

## Consequences

- **Easier:** M2's allowlists and M4's origin filters find their columns; attacks take production's retrieval path; tests pin the block.
- **Harder:** the loader writes as the owner: a second `set_config` call site, guarded by a role check and a name scan that `getattr()` defeats. A new origin needs a migration and this block.
- **Watch (M3, Timur's condition):** in Azure the loader gets its own identity, and the app's identity can obtain neither that login nor the migrator's.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 1, questions 1 and 2, defaults 5 and 12 (2026-11-08).
- `docs/reports/m0.md`, "M1 handovers": PR 5b product sheets, PR 8 sec-1, sec-2, sec-3, sec-8 and spec-4 (2026-11-08).
- `data/synthetic/generate.py`, `ORIGIN` and `trust_for()`, at `f866a58` (2026-11-08).
- PostgreSQL 17, "Generated Columns" (a generated column cannot be written), https://www.postgresql.org/docs/17/ddl-generated-columns.html (accessed 2026-11-08).
- PostgreSQL 17, "Row Security Policies" (owners bypass unless FORCE; no policy means default deny; TRUNCATE is not subject to row security), https://www.postgresql.org/docs/17/ddl-rowsecurity.html (accessed 2026-11-08).
