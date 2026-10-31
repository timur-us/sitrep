# SITREP

**SITREP** (situation report) is an advisor copilot for **Feistel Private Bank**, a fictional German private bank. It prepares a relationship manager for a client call with cited answers over their own mandates, drafts the follow-up, and executes side effects only after human approval — built around an own agent harness and red-teamed against real attack patterns.

> Fictional bank, synthetic data. Nothing here is a real institution, person or record.

*Know your client before the call.*

Status: M0 in progress. See `docs/brief.md`, `docs/threat-model.md`, `docs/discovery.md` and `redteam/SCOPE.md`. Decisions are recorded in `docs/decisions/` (ADRs).

## Local database

`make up` starts PostgreSQL 17 with pgvector in Docker Compose, `make migrate` applies `db/migrations` as the migration role, and `make test` needs both. The database uses trust auth for all connections into the container, reachable only through a 127.0.0.1 publish (port 55432): a local convenience for synthetic data. Azure is Entra-only from M3. On Linux, use Docker Engine 28 or later; earlier engines expose localhost-published ports to the local network segment. To see the server, run `docker compose exec db psql -U postgres -d sitrep -c 'SELECT version()'` (PostgreSQL 17.11). Roles and migrations: ADR 0008 and `docs/runbooks/local-setup.md`. The four tables (`advisors`, `mandates`, `documents`, `chunks`) have row-level security, forced on their owner too: the app role sees an advisor's rows only inside a transaction that names that advisor, and zero rows otherwise. The residual risk (a forged `set_config` through SQL injection) and its mitigations are in `docs/threat-model.md`.

## What this project shows, and what it does not

This section is a promise for the finished project. Each item names the milestone that delivers it; until then it is a plan, not a result.

### Shows

- **An own agent harness** on the Anthropic Messages API: a tool registry with declared effects, side effects only after the advisor approves, untrusted retrieval results wrapped with provenance, and traces without prompt plaintext (interfaces and traces M0, the loop M1, hardening M2; ADR 0001, ADR 0007).
- **An advisor boundary enforced by the database**: row-level security in PostgreSQL scopes every query to the logged-in advisor's own mandates, not the prompt (M0 schema and tests, M1 retrieval).
- **Measured red teaming**: the attack success rate of the EchoLeak pattern (prompt injection through an e-mail) in English and German, before and after each hardening layer, from a recorded corpus (M1 unhardened, M2 hardened).

### Does not show

- **Production readiness.** It is a single-region demo with a small Azure footprint from M3, not a bank system.
- **Real data.** The bank is fictional and every record is synthetic. No real client data, mailbox or CRM is ever connected.
- **Legal advice.** The regulatory notes in `docs/brief.md` are an engineering view.
- **EU-only inference.** The default route to the model has no EU-only option; the exits are documented in ADR 0003.
- **Network-level egress control.** No VNet, NAT or firewall; egress is controlled in the application only (`docs/threat-model.md`).
