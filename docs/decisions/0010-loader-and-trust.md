# ADR 0010: The loader, origin and trust, product sheets, attack ids

## Status

Accepted (Timur, 2026-11-08, M1 plan PR 1 and question 2); Fable's design read before the merge.

## Date

2026-11-08

## Context

SITREP (situation report) loads its synthetic corpus in M1 (PR 7). M0 handed over its shape: runtime text is always untrusted (PR 8 sec-1); product sheets fit no advisor-scoped table (PR 5b); attack ids must be distinct, and no policy may admit `advisor_id IS NULL` (sec-8); `origin` and `trust` become columns (spec-4). The app role only reads and FORCE binds the owner, so the loader writes as the owner (question 2).

## Options

- **Trust written by the loader:** one wrong writer makes an e-mail `internal`.
- **A trust lookup table:** trust becomes rows the owner can update.
- **Product sheets in `documents`** under `OR advisor_id IS NULL`: the bypass shape sec-8 forbids. **Copied per advisor:** three copies to keep equal.
- **Attack documents in their own table:** a path production never reads.
- **A loader role now:** Timur put it in M3.

## Decision

- **`origin`** is `text NOT NULL` with a CHECK, on every table holding document text (the block's `tables`). `chunks` inherit it through the foreign key `(advisor_id, document_id)`. Structured tables (`advisors`, `mandates`, `mandate_contacts`) carry none.
- **`trust`** is a STORED generated column (default 12): `internal` for the block's internal origins, else `untrusted`, so a new origin fails closed. No writer sets it. Trust is provenance, never permission: every hit is still `Untrusted` (rule 9).
- **Runtime text** (`drafts`, `followups`, PR 14) is `untrusted`, unretrieved in M1 (default 5).
- **`product_sheets`** is the one global table: no `advisor_id`; ENABLE and FORCE; the app role reads every row while `app.advisor_id` is set; only the owner writes. No policy contains `advisor_id IS NULL`. Each sheet stores its exact `https` URL, no query, fragment or user part: M2's link allowlist (sec-3). A sheet is one embedding; one over the chunk cap is refused, never cut.
- **`mandate_contacts`** holds each mandate's recipients under row-level security, from the mandate record's `client_email`, never from e-mail headers: M2's recipient allowlist (sec-2). Mailbox text cannot add a recipient.
- **Attack documents** have ids starting `rt-`, compared case-insensitively. The loader refuses a corpus id with the prefix, an attack id without it, and a duplicate across both. They load only on an explicit flag, into the case's target mandate, as `mailbox` whatever the case says; a load without the flag leaves none.
- **The loader's identity:** the migrator after `SET ROLE` to the owner, one advisor per transaction. `owner_advisor_scope()` in `app/db/session.py` sets the advisor and refuses any session but the migrator acting as the owner; an AST scan keeps request code from it. WITH CHECK and the composite foreign keys refuse a row of another advisor.
- **Loading:** every sha256 is checked against the manifest before the first write; each advisor's rows are replaced in one transaction. Chunks hold at most 400 tokens, with model id and revision per vector.

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

- **Easier:** M2's allowlists and M4's origin filters find their columns; the red team attacks production's retrieval path; `tests/test_docs.py` pins the generator to the block, PR 7 the schema.
- **Harder:** the loader writes as the owner with the migrator's login: a second `set_config` call site, guarded by a role check and a name scan that `getattr()` defeats. A new origin needs a migration and this block. `internal` is where text came from, not that it is true.
- **Watch (M3, Timur's condition):** in Azure the loader gets its own identity, separate from the migrator.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 1, questions 1 and 2, defaults 5 and 12 (2026-11-08).
- `docs/reports/m0.md`, "M1 handovers": PR 5b product sheets, PR 8 sec-1, sec-2, sec-3, sec-8 and spec-4 (2026-11-08).
- `data/synthetic/generate.py`, `ORIGIN` and `trust_for()`, at `f866a58` (2026-11-08).
- PostgreSQL 17, "Generated Columns" (a generated column cannot be written), https://www.postgresql.org/docs/17/ddl-generated-columns.html (accessed 2026-11-08).
- PostgreSQL 17, "Row Security Policies" (owners bypass unless FORCE; no policy means default deny), https://www.postgresql.org/docs/17/ddl-rowsecurity.html (accessed 2026-11-08).
