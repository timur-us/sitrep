# ADR 0002: pgvector in PostgreSQL over Azure AI Search

## Status

Accepted (Timur, 2026-10-28, in the M0 plan).

## Date

2026-10-29

## Context

SITREP (situation report) retrieves from an advisor's own mandates only, and that boundary must be enforced by the database, not by the prompt (CLAUDE.md rule 8, threat-model path 2). The public Azure footprint should stay under 25 EUR per month (`docs/discovery.md`). Retrieval is hybrid: full-text search in German and English, plus vectors.

## Options

- **Azure AI Search** next to PostgreSQL. Managed hybrid ranking. Basic costs about 64.90 EUR per month (0.0889 EUR per hour in Germany West Central and West Europe), well over the budget on its own. Free has 50 MB, no SLA, and may be deleted after long inactivity. The advisor filter would be a query parameter our code sets, a second store to keep in step, and a second place where the boundary can be wrong.
- **pgvector in the same PostgreSQL.** Burstable B1ms with 32 GiB storage costs about 16.6 EUR per month in Germany West Central (12.78 compute plus 3.86 storage). Row-level security covers vectors and text in one policy. We own the ranking.

## Decision

pgvector in PostgreSQL: 0.8.7 on PG 17 locally (digest-pinned image, PR 5a), Azure Database for PostgreSQL Flexible Server from M3.

## Consequences

- **Easier:** one store, one boundary. RLS with FORCE sits next to the data and binds every query, vector or full-text (PR 5b). One bill under the target.
- **Harder:**
  - We own hybrid ranking (full-text plus vector fusion) and its evals.
  - HNSW filters after the index scan, so under RLS a query can return fewer rows than asked for. M1 needs `hnsw.iterative_scan` (pgvector 0.8.0 and later) and a test for it.
  - Azure offers pgvector 0.8.2 on PG 17, five patch releases behind the local 0.8.7. M3 re-checks the difference.
  - Azure needs `vector` on the `azure.extensions` allow-list: an M3 what-if.
  - Burstable is "not recommended for production workloads". It is right for a demo and wrong for a bank.
- **Watch:** if retrieval quality in M1 needs semantic re-ranking we cannot build, revisit with a cost line.

## Sources

- Azure Retail Prices API, AI Search Basic and PostgreSQL Flexible Server B1ms and storage, Germany West Central and West Europe, https://prices.azure.com/api/retail/prices (queried 2026-10-29).
- Microsoft, "Service limits in Azure AI Search", https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity (page date 2026-09-16).
- Microsoft, "Compute options in Azure Database for PostgreSQL", https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute (page date 2026-07-08).
- Microsoft, "Extension versions", https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions (page date 2026-07-10).
- Microsoft, "Allow extensions", https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-allow-extensions (page date 2026-07-10).
- pgvector README (filtering, iterative index scans), https://github.com/pgvector/pgvector (accessed 2026-10-29), and CHANGELOG, 0.8.7 released 2026-10-01, https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md (accessed 2026-10-29).
