# ADR 0008: Data access: psycopg 3, SQL migrations, three roles

## Status

Accepted (Timur, 2026-10-28, in the M0 plan; recorded and amended by its review in M0 PR 5a).

## Date

2026-10-30

## Context

SITREP (situation report) keeps every advisor's mandates in one PostgreSQL database. Threat-model path 2 and CLAUDE.md rule 8 put the advisor boundary there: an app role that owns nothing, no BYPASSRLS, FORCE ROW LEVEL SECURITY, the advisor id per transaction from the verified login. That needs a sound role layout and connection code. Locally: trust auth; Azure (M3): Entra-only.

## Options

- **SQLAlchemy with Alembic.** It hides the SQL that grants, policies and `set_config` depend on: a second layer to audit.
- **asyncpg.** Its URL parser is not libpq's, so a loopback check could disagree with the connection.
- **psycopg 3, numbered SQL files, a short runner.** Server-side binding, libpq's own parser, `psycopg_pool` with check callbacks. We write every query and own the runner.

## Decision

The third option: roles and runner in PR 5a; tables, policies and pool in PR 5b.

- **Driver:** `psycopg[binary]` 3.3.6, bundling libpq 18. Only `app/db/connect.py` opens connections.
- **URLs**, parsed by libpq's `conninfo_to_dict`: exactly `user`, `host` (`127.0.0.1`, one host), `port` and `dbname`, all required, nothing else. `DATABASE_URL` logs in as the app role, `MIGRATION_DATABASE_URL` as the migrator, both to `sitrep`. Rejected values are not echoed.
- **`connect()`** refuses while any `PG*` variable is set (libpq would fill or override parameters from them), takes no connection parameter as a keyword, and pins `hostaddr` and `options` as a second line.
- **Roles** (`db/init/00-roles.sql`, every attribute written out):
  - `sitrep_owner`: NOLOGIN; owns the database, the schemas and everything in them;
  - `sitrep_migrator`: LOGIN; holds the owner `WITH INHERIT FALSE`, so it acts as the owner only after `SET ROLE`;
  - `sitrep_app`: LOGIN; no attributes, no memberships, owns nothing, CONNECT on `sitrep` only.

  The superuser creates `vector` (not trusted) and owns `public`. PUBLIC loses CONNECT on `postgres` and `template1`, and the large-object creators. The owner's future functions and types are not PUBLIC (default privileges).
- **Migrations:** numbered SQL files, forward only, applied by `python -P -m app.db.migrate`. The runner refuses any login but the migrator and records sha256, `session_user` and `current_user` per file. A changed or missing applied file stops the run before any DDL. New files run in one transaction, under a lock; a file that ends it, or changes role or `search_path`, is refused unrecorded (detected, not prevented).
- **For PR 5b, decided here:**
  - the advisor context is `SELECT set_config('app.advisor_id', %s, true)` inside a real `BEGIN`, the bindable form of `SET LOCAL`;
  - primary keys `(advisor_id, id)`, and `advisor_id` in every unique index (closing the referential-integrity channel);
  - the app role gets `SELECT` only in M0;
  - pool callbacks reject any connection but the app role, attribute- and membership-free, advisor setting empty.
- **Local port:** 55432, not 5432, where a native PostgreSQL may answer (Timur, 2026-10-30).

## Consequences

- **Easier:** readable SQL, checked by `tests/db/` as each role; each migration hashed and attributed.
- **Harder:** hand-written SQL, our own runner, no down migrations. Trust auth lets any local process connect as any role, the superuser too.
- **Watch:**
  - placeholder settings are USERSET: SQL injection could call `set_config` (5b's residual risk);
  - a role can `ALTER ROLE` its own defaults; 5b's checkout guard checks the session;
  - M3's Entra token must reach `connect()` as an allowed option, not in the URL;
  - Azure has pgvector 0.8.2, here 0.8.7;
  - pip-audit does not see CVEs in the libpq and OpenSSL that psycopg-binary bundles: treat its bumps as security updates.

## Sources

- `docs/plans/m0-foundation.md`, "Defaults", "Postgres and RLS", PR 5a and PR 5b (2026-10-28).
- `docs/threat-model.md`, path 2 (2026-10-27).
- PostgreSQL 17 documentation, `GRANT` (membership options `INHERIT` and `SET`) and libpq's environment variables, https://www.postgresql.org/docs/17/. The behaviour relied on here is measured in `tests/db/` on `pgvector/pgvector:0.8.7-pg17-bookworm` (PostgreSQL 17.11) with psycopg 3.3.6 and libpq 18 (2026-10-30).
- psycopg 3.3.6 on PyPI, uploaded 2026-09-18, https://pypi.org/project/psycopg/ (lock resolved 2026-10-30).
