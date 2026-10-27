# Red-team scope

Date: 2026-10-27 · Owner and sole authoriser: Timur Usipbekov · Status: in force for the life of the repository

## Target

Only the application in this repository (SITREP), running locally or in the project's own Azure resource group, with the synthetic corpus produced by `data/synthetic/generate.py`. Nothing else is a target.

## In scope

- Prompt injection through documents and e-mails in the synthetic corpus (indirect, zero-click).
- Exfiltration attempts through links, images, tool arguments and outgoing requests, measured by the canary token under `canary.example`.
- Cross-mandate access attempts between the synthetic advisors Alice, Bob and Carol, including through tool arguments and forged identity headers.
- Approval bypass attempts (forged or reused approval tokens).
- Cost and loop abuse against the harness limits.
- Poisoning of the synthetic knowledge base.

## Out of scope — always

- Any system, service, account, mailbox or data that is not this repository's own application and synthetic corpus.
- The safety or abuse systems of Anthropic, Microsoft, GitHub or any other provider. We test *our* harness, not theirs.
- Real people, real companies, real domains. The attacker is `mallory.example`; staff are under `feistel.example`.
- Denial of service against anything shared, including the Anthropic API or Azure.
- Any technique that would be illegal if the target were real.

## Rules

- Attacks are data in `redteam/attacks/`, replayed from recordings in CI. Live runs only by hand after a cost estimate and an explicit yes.
- Every run writes a report to `redteam/results/` with the attack success rate, the trace ids and the layer that should have caught each success.
- A finding against a provider (not our code) is not pursued; it is noted and, if it looks like a real vulnerability, reported to the provider through its disclosure channel.

## Stop criteria

Stop the run immediately and tell Timur if: any real-looking personal data, credential or domain appears anywhere; a request would leave the defined provider path; a test would touch a resource outside the project's resource group; or a result cannot be explained by the harness under test.
