# SITREP — discovery

Date: 2026-10-27 · Status: simulated engagement · Owner: Timur
**This document is a simulation.** Feistel Private Bank is fictional; the interviews below are written from the perspective of the roles a real discovery would meet, to make the requirements concrete and testable. No real bank, person or data is involved.

## Engagement frame

- Client: Feistel Privatbank AG (fictional), wealth management, about 100 relationship managers.
- Sponsor: head of wealth management. Pilot team: three advisors (Alice, Bob, Carol) with their own mandates.
- Question to answer in discovery: where does advisor time go before and after a client call, what may a copilot see and do, and what would the bank need to see before it trusts it.

## Interviews (simulated)

**Alice, relationship manager, 120 mandates.** "Before a call I open five things: KYC, the portfolio report, my last note, the mailbox, and whatever product the client asked about. Thirty minutes if I'm quick. I miss an e-mail once a week. After the call I write the note and the summary to the client, and the follow-up task gets lost in my calendar." Wants: one page before the call, with sources she can click; drafts she edits, not texts she has to write. Fears: a wrong figure in a client e-mail, and anything that sends without her.

**Team lead, wealth management.** "I need to know whether the team uses it, whether answers are right, and that nobody sees mandates that aren't theirs. If one advisor leaves and takes client lists, that's my problem." Wants: usage and quality numbers per week; a hard boundary between advisors. Fears: adoption theatre, where it's rolled out and nobody uses it.

**Compliance officer.** "Show me what the system did, for which client, on whose behalf, and that the client was not advised by a machine. If a figure in a client communication is wrong, I need to find out why in five minutes." Wants: a trace per answer, approvals logged, AI drafts marked as such (Art. 50), a DORA register entry for the provider. Fears: client data leaving the bank in ways nobody planned.

**IT security.** "Everything that reads our mailboxes is an attack surface. Anyone in the world can put text in front of your model by sending an e-mail. Show me that you've tried to break it before we talk about a pilot." Wants: a threat model, a red-team report with numbers, identity from Entra only, no shared passwords, no admin accounts. Fears: a copilot that leaks through a link.

## Pain points, ranked

1. Preparation time per call (20–40 minutes) and missed context.
2. Manual post-call work (note, follow-up, summary).
3. No trace of what was looked at or sent.
4. Boundary between advisors enforced by habit, not by systems.

## Success criteria for the pilot (measured in the synthetic usage log, labelled as simulation)

- Preparation time per call: from 20–40 minutes to under 10, measured as time from first question to "ready".
- Share of client-facing drafts accepted with light edits: above 70 %.
- Grounding: 100 % of figures in answers traceable to sources (number guard); eval gate green on every release.
- Boundary: 0 cross-mandate reads in the red-team report; forged identity header: 0 effect.
- Attack success rate for the EchoLeak family: measured before hardening, under 5 % after, with the remaining cases explained.
- Every answer has a trace id; every approval is logged with who, what, when.

## Constraints

- Identity from the bank's Entra ID; no local accounts in production.
- Client data never leaves the defined provider path; provider switch possible by configuration (DORA exit).
- No investment advice to clients; drafts marked as AI-generated; the advisor decides.
- Budget: a small Azure footprint (target under 25 € per month while public), torn down when not in use.

## What discovery changes in the plan

The security interview moved the red-team work *before* the pilot discussion: the first measurable attack result is part of the MVP (M1), not a later add-on. Compliance's "five minutes to find out why" became the tracing requirement: trace id in every answer, approvals logged, no plaintext prompts in central logs.
