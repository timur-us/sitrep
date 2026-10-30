# ADR 0008: Data access: psycopg 3, SQL migrations, three roles

## Status

Proposed. The design was accepted with the M0 plan (Timur, 2026-10-28); this record is accepted with the merge of M0 PR 5a.

## Date

2026-10-30

## Context

SITREP (situation report) keeps every advisor's mandates in one PostgreSQL database. Threat-model path 2 and CLAUDE.md rule 8 put the advisor boundary in the database: an app role that owns nothing and has no BYPASSRLS, FORCE ROW LEVEL SECURITY, and the advisor id set per transaction from the verified login. That holds only on a sound role layout and connection code. Locally the database uses trust auth; Azure (M3) is Entra-only.

## Options

- **SQLAlchemy with Alembic.** It hides the SQL that grants, policies and `set_config` depend on: a second layer to audit.
- **asyncpg.** Its URL parser is not libpq's, so a loopback check could disagree with the connection.
- **psycopg 3, numbered SQL files, a short runner.** Server-side binding by default, libpq's own parser for the URL check, `psycopg_pool` with check callbacks. We write every query and own about 150 lines of runner.

## Decision

The third option: PR 5a builds roles and runner, PR 5b tables, policies and pool.

- **Driver:** `psycopg[binary]` 3.3.6, which bundles libpq 18. `app/db/connect.py` is the only place that opens a connection.
- **URLs:** `DATABASE_URL` and `MIGRATION_DATABASE_URL` name one host, exactly `127.0.0.1`, and only `user`, `host`, `port` and `dbname`: no password, `hostaddr`, `options` or `service`. libpq's `conninfo_to_dict` parses them. `connect()` passes `hostaddr=127.0.0.1` and an empty `options`, so `PGHOSTADDR` and `PGOPTIONS` cannot redirect the connection or add server settings. Rejected values are not echoed.
- **Roles** (`db/init/00-roles.sql`, every attribute written out):
  - `sitrep_owner`: NOLOGIN; owns the database, the schemas and everything in them;
  - `sitrep_migrator`: LOGIN; holds the owner `WITH INHERIT FALSE`, so it acts as the owner only after `SET ROLE`;
  - `sitrep_app`: LOGIN; no attributes, no memberships, owns nothing, CONNECT on `sitrep` only.

  The superuser creates `vector` (not trusted) and owns `public`.
- **Migrations:** numbered SQL files, forward only, applied by `python -P -m app.db.migrate`. The runner refuses any login but the migrator and records sha256, `session_user` and `current_user` per file. A changed or missing applied file stops the run before any DDL. New files run in one transaction, under a lock.
- **For PR 5b, decided here:**
  - the advisor context is `SELECT set_config('app.advisor_id', %s, true)` inside a real `BEGIN`, the bindable form of `SET LOCAL`;
  - primary keys are `(advisor_id, id)`, and every unique index includes `advisor_id`, which closes the referential-integrity covert channel;
  - the app role gets `SELECT` only in M0;
  - pool callbacks reject a connection unless it is the app role, without attributes or memberships, with the advisor setting empty.
- **Local port:** `127.0.0.1:55432`, not 5432, so a native PostgreSQL never answers in its place (Timur, 2026-10-30).

## Consequences

- **Easier:** the boundary is readable SQL, and `tests/db/` checks it as each role. Each migration has a hash and a recorded author.
- **Harder:** hand-written SQL and our own runner. No down migrations: a fix is a new file. Trust auth lets any local process connect as any role, the superuser included.
- **Watch:**
  - placeholder settings are USERSET, so SQL injection as the app role could call `set_config`. PR 5b records this residual risk;
  - M3 passes Entra tokens as passwords, which the URL check forbids; the token must arrive as a connect argument, not in the URL;
  - Azure offers pgvector 0.8.2 on PostgreSQL 17, against 0.8.7 here;
  - other `PG*` variables (TLS, service file) are not pinned.

## Sources

- `docs/plans/m0-foundation.md`, "Defaults", "Postgres and RLS", PR 5a and PR 5b (2026-10-28).
- `docs/threat-model.md`, path 2 (2026-10-27).
- PostgreSQL 17 documentation, `GRANT` (membership options `INHERIT` and `SET`) and libpq's environment variables, https://www.postgresql.org/docs/17/. The behaviour relied on here is measured in `tests/db/` on `pgvector/pgvector:0.8.7-pg17-bookworm` (PostgreSQL 17.11) with psycopg 3.3.6 and libpq 18 (2026-10-30).
- psycopg 3.3.6 on PyPI, uploaded 2026-09-18, https://pypi.org/project/psycopg/ (lock resolved 2026-10-30).
