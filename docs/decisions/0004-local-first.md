# ADR 0004: Local first, Azure from M3

## Status

Accepted (Timur, 2026-10-28, in the M0 plan).

## Date

2026-10-29

## Context

SITREP (situation report) gets about two hours a day (`docs/brief.md`). The properties that matter most (the advisor boundary in the database, no prompt plaintext in logs, no egress, approvals before side effects) do not depend on Azure. Cloud rights are themselves an attack path: a PR workflow with a cloud login, or an agent that can assign roles (threat-model paths 9 and 10). Azure also costs money every day it runs.

## Options

- **Azure from day 1.** Entra ID and managed identity are real from the first PR. Every iteration waits on deploys, the agent needs some route to Azure from the start, and the bill runs while M1 is still being built.
- **Local first, Azure in M3.** M0–M2 run on Docker Compose and a dev auth shim, with CI offline. Azure arrives once the boundary, the harness and the hardening are measured locally.

## Decision

- M0–M2 local: PostgreSQL with pgvector in Docker Compose, the dev auth shim on loopback, CI with no cloud login and no API key.
- Azure in M3, with Entra ID, managed identity, Key Vault and OIDC from the protected `production` environment.
- **Agent access:** the agent has no Azure login in M0–M2 (`AZURE_CONFIG_DIR` points at an empty folder; `docs/github-setup-checklist.md`). How the agent reaches Azure in M3, and the what-if parameter file, get their own ADR, with the next free number after 0009.

## Consequences

- **Easier:** fast loops, no cloud bill, and no cloud credential anywhere near the agent or a pull request while most of the code is written.
- **Harder:** Entra and managed-identity surprises (token audiences, role propagation delay, platform headers) arrive late, in M3. The local trust auth of PostgreSQL is a convenience that Azure, Entra-only, does not have, so the role and connection code meets a different auth path in M3.
- **Mitigation:** M3 keeps a buffer, and the forged-header test (threat-model path 3) is written before the Azure auth is trusted.
- **Watch:** anything built in M0–M2 that assumes a local-only property, such as a loopback bind or trust auth, is listed in the M3 plan.

## Sources

- `docs/brief.md`, milestones and time budget (2026-10-27).
- `docs/threat-model.md`, paths 3, 9 and 10 (2026-10-27).
- `docs/github-setup-checklist.md`, "Local" and "Environment `production`" (2026-10-27).
- `docs/plans/m0-foundation.md`, "Decisions taken" (2026-10-28).
