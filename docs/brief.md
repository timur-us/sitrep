# SITREP — project brief

Date: 2026-10-27 · Status: accepted plan, nothing built yet · Owner: Timur
SITREP (situation report) is an advisor copilot for **Feistel Private Bank**, a fictional German private bank (legal name Feistel Privatbank AG). Everything in this repository is synthetic. Tagline: *Know your client before the call.*

## Users

Relationship managers in wealth management, 80–150 mandates each (a mandate is one client relationship, e.g. the Weber family with the advisor Alice). Secondary: the team lead (adoption, quality), compliance (audit trail), IT security (approvals, attacks).

## Problem

Before every client call the advisor pulls together what matters from five places: the KYC profile, the portfolio report, the notes of the last conversation, the client's recent e-mails and the product sheet the client asked about. That takes 20–40 minutes per call, is error-prone and leaves no trace. After the call, the note, the follow-up tasks and the summary to the client are written by hand.

## Solution

SITREP answers "What do I need to know before the call with Weber?" with a cited one-pager built from the advisor's *own* mandates (retrieval with access control enforced in the database), and drafts the call note, the follow-up and the e-mail afterwards. Anything with a side effect (sending, creating a task) happens only after the advisor approves it in the UI. Every answer carries sources, a trace id, cost and the result of the number guard (every figure in the answer must appear in the sources — the pattern from ForecastPilot).

## Scope of the MVP

- Knowledge base: synthetic documents for 3 advisors (Alice, Bob, Carol) and about 40 mandates: KYC profile, portfolio report, call notes, product sheets, mailbox.
- Chat UI served by the app; the advisor sees only their own mandates, enforced by row-level security in PostgreSQL, not by the prompt. Login: a development shim locally, Entra ID in Azure.
- Agent with four tools: `search_client_docs`, `get_portfolio`, `draft_email` (approval), `create_followup` (approval).
- Harness: tool registry with declared effects and data classes, approval gate with server-issued single-use tokens, context layer with provenance wrapping and token budgets, output policy (link, image and recipient allowlists, canary scan), tracing without prompt plaintext, eval harness with a regression gate, red-team harness with a recorded attack corpus.
- First attack path: the EchoLeak pattern (zero-click prompt injection through an e-mail, exfiltration through a link, an image or a tool argument).

## Non-goals

- No investment advice to the end client, no order execution, no suitability assessment (MiFID II stays with the advisor).
- No creditworthiness assessment of natural persons (Annex III of the EU AI Act is deliberately avoided).
- No real client data at any time; no connection to real mailboxes or CRM systems.
- No multi-tenant SaaS, no mobile app, no fine-tuning.
- No testing of systems that are not this repository; red teaming only within `redteam/SCOPE.md`.

## Run as an engagement, not a demo

The project is run like a customer engagement, with synthetic stakeholders: `docs/discovery.md` (interviews, pain points, success criteria, constraints), `docs/decisions/` (ADRs), `docs/threat-model.md`, `docs/rollout.md` (pilot, training under Art. 4 AI Act, feedback loop) and `docs/adoption.md` (metrics from a synthetic usage log, labelled as a simulation). The README closes, as ForecastPilot does, with what the project shows and what it does not.

## Regulatory frame (engineering view, not legal advice)

- **GDPR.** Client data in prompts means processing by the LLM provider: a data processing agreement (Art. 28), a data protection impact assessment (Art. 35) is likely required, data minimisation (only the advisor's own mandate in context; pseudonymisation as an option), traces that contain prompts are personal data (short retention, no plaintext in central logs). Art. 22 does not apply while the advisor decides. Data residency drives the provider choice.
- **DORA** (in force since 17 January 2025). The copilot is an ICT service and the LLM provider an ICT third-party provider: register of information, contractual minimums, exit strategy (provider switch by configuration), incident reporting, testing. The red-team harness answers the testing duty, the trace pipeline the evidence duty.
- **EU AI Act.** An advisor copilot without creditworthiness assessment is not a high-risk system under Annex III; the bank documents that classification. Transparency duties under Art. 50 apply since 2 August 2026 (AI drafts to clients are marked as such). AI literacy under Art. 4 applies since 2 February 2025. The Annex III high-risk obligations were moved to 2 December 2027 by the Digital Omnibus (Council approval 29 June 2026).
- **Supervision.** MaRisk (BaFin) for outsourcing and IT risk; MiFID II for investment advice. Both stay with the advisor and the bank.

## Milestones (about 2 hours per day)

| Milestone | Days | Outcome |
|---|---|---|
| M0 foundation, local | 1–3 | repo with guardrails, CI with all gates (evals and red team offline), Postgres with pgvector and RLS via Docker Compose, dev auth shim, FastAPI skeleton with `/health` and tracing, harness interfaces with tests, synthetic data generator, ADRs 0001–0006 |
| M1 MVP core, local | 4–9 | retrieval with RLS, agent loop with four tools, approval gate, UI from the app, evals v1 (recorded), first red-team case unhardened with a measured success rate — **demo on day 9** |
| M2 hardening, local | 10–14 | output policy, canaries, provenance wrapper, recipient allowlist, automated red-team harness (15–20 variants, recorded), cross-mandate case, eval gate in CI, README with before/after — **MVP done** |
| M3 Azure with full identity | 15–21 | Bicep, Entra ID login, managed identity, Key Vault, OIDC from a protected GitHub environment, forged-header test, production proof |
| M4 customer layer and close | 22–28 | rollout and adoption docs, cost dashboard, DORA/GDPR mapping, second attack path (poisoning), Foundry switch by configuration (stretch), screenshots, README close |

M2 is never cut; M4 may be.
