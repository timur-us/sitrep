# ADR 0003: Anthropic API by default, Foundry as a configuration switch

## Status

Accepted (Timur, 2026-10-28, in the M0 plan, question 4).

## Date

2026-10-29

## Context

SITREP (situation report) sends client data to the model at runtime: Sonnet 5.5, and Haiku 5.5 as an extra guard layer. Under DORA the model provider is an ICT third-party provider, so the bank needs a documented exit, and under GDPR the data's location drives the provider choice (`docs/brief.md`). A German private bank would ask for EU-only processing first.

## Options

- **Anthropic API (direct).** First access to features, one contract, the simplest client. Inference geography is `global` or `us` only; there is no EU-only option.
- **Microsoft Foundry.** Claude inside the Azure tenant, billed through Azure. Global Standard or the US Data Zone; the Europe Data Zone is "Not available". The vendors disagree on Sonnet 5.5: Microsoft's table lists it in the US Data Zone, Anthropic's page says it supports only Global Standard.
- **Amazon Bedrock `eu.*` or Google Vertex AI `eu`.** Both offer EU-only processing for Sonnet 5.5 and Haiku 5.5 (Bedrock routes within six EU regions; Vertex lists a Europe multi-region). The cloud provider becomes the processor, and it adds a second cloud next to Azure.

## Decision

- The direct Anthropic API is the default route.
- One client factory builds every client. Foundry is a configuration switch in that factory, not a code change: that switch is the DORA exit, built as a stretch goal in M4.
- No dependency on the Batches API, the Models API or server-side fallbacks, so that the switch stays a switch.
- Stated plainly: **there is no EU-only inference on the default route or on Foundry.** The documented EU exits are Bedrock `eu.*` and Vertex `eu`.

## Consequences

- **Easier:** one client, one key, one bill in M1; the exit is designed in from the start.
- **Harder:** a real bank would likely need the EU exit, and taking it means a new processor, a new contract and new evals. This project does not show EU-only inference (README).
- **Retention, where the sources disagree.** Anthropic's privacy center says API inputs and outputs are deleted within 30 days. Anthropic's API docs say conversation content is not retained by default, except for covered models, which require 30-day retention. Both are cited; the DORA register entry in M4 must quote the contract, not either page.
- **Watch:** an EU inference geography on the direct API or an EU Data Zone on Foundry would change this decision.

## Sources

- Anthropic, "Data residency", https://platform.claude.com/docs/en/manage-claude/data-residency (accessed 2026-10-29).
- Microsoft, "Foundry Models from partners and community", https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-from-partners (page date 2026-09-21).
- Anthropic, "Claude in Microsoft Foundry", https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry (accessed 2026-10-29).
- AWS, Bedrock model card for Claude Sonnet 5.5 (geo inference id `eu.anthropic.claude-sonnet-5-5`), https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html (accessed 2026-10-29).
- AWS, Bedrock model card for Claude Haiku 5.5 (geo inference id `eu.anthropic.claude-haiku-5-5`), https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html (accessed 2026-10-29).
- Google Cloud, Claude Sonnet 5.5 on Vertex AI, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5-5 (page date 2026-10-28).
- Google Cloud, Claude Haiku 5.5 on Vertex AI (Europe multi-region), https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/haiku-5-5 (page date 2026-10-28).
- Anthropic Privacy Center, "How long do you store my organization's data?", https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data (page date 2026-07-01).
- Anthropic, "API and data retention", https://platform.claude.com/docs/en/manage-claude/api-and-data-retention (accessed 2026-10-29).
