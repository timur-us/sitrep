# ADR 0013: Retrieval: hybrid ranking, the iterative scan, the handle

## Status

Accepted in outline (Timur, 2026-11-08: the M1 plan, PR 8, default 13). Its departures from the plan (no GIN index, no report date) await his yes, the text Fable's review.

## Date

2026-11-10

## Context

SITREP (situation report) answers from an advisor's own documents and the bank's product sheets (ADR 0010), in German and English (ADR 0005), under row-level security (ADR 0008). Measured as the app role (PostgreSQL 17.11, pgvector 0.8.7):
- **`@@` and `<=>` are not leakproof**, so the policy runs first and neither becomes an index condition. A planted GIN index on `tsv` serves the superuser, never the app role, which reads through the primary key (`advisor_id` first) and filters.
- **HNSW sits idle here:** the planner sorts the advisor's rows exactly, even beside 6,000 of another's. Pushed onto HNSW while the policy hides the nearest rows, a search returns 0 of 8 rows with `hnsw.iterative_scan` off, 8 with it on.
- **500 characters can tokenize to 2,005 tokens;** the model takes 512.
- **PR 5b sec-5:** the advisor transaction yielded the pool's connection.

## Options

- **One fused SQL statement:** fewer round trips; fusion untestable without a database.
- **Vectors only:** miss exact names and figures; e5 is weaker from German to English (PR 4).
- **GIN indexes, as planned:** a write cost for an index no app query can use. LEAKPROOF needs a superuser, which Azure's Flexible Server lacks.
- **Cutting a long query:** silent; refused instead, as PR 4 refuses long passages.

## Decision

- **Ranking** (`app/harness/handlers.py`): three lists of at most 20 records, full-text in English and in German (each keeping to its language) and the nearest vectors of the query's model, a document by its nearest passage; reciprocal rank fusion, k = 60, ties by kind and id. Sheets join each list unless a `mandate_id` limits the search to that mandate's documents.
- **A mandate id** is looked up inside the transaction: another advisor's is "no such mandate", like an unknown one (no hits, no portfolio).
- **A hit** carries kind, mandate (none for a sheet), title, language, trust from its table (never from an id; PR 7 sec-7) and the stored passage nearest the query, whole. Another advisor's row is refused: a second line behind the policies.
- **`get_portfolio`** reads the `portfolio_system` document, so no e-mail poses as the report. `Portfolio` drops `as_of`: no document stores a date.
- **The iterative scan:** `relaxed_order`, transaction-local beside the advisor (default 13), pgvector's defaults otherwise; rows sorted again outside a `MATERIALIZED` CTE by `distance + 0`, since PostgreSQL 17 otherwise trusts the CTE's order (measured).
- **No index on `tsv`**, nor on the six sheets' vectors.
- **Limits:** queries of 1–500 characters, refused as `query_too_long` over the model's limit, before any connection; 1–20 hits.
- **The handle:** the block yields an `AdvisorConnection` returning fully fetched rows and raising `TransactionEnded` after the block, inside the next advisor's transaction too.

```yaml
rrf_k: 60
candidates_per_list: 20
kinds: [document, product_sheet]
max_limit: 20
max_query_chars: 500
iterative_scan: relaxed_order
query_too_long: query_too_long
```

## Consequences

- **Easier:** deterministic ranking, tested without a database; per search one embedding, holding no connection, and nine statements; nothing outlives the block.
- **Harder:** full-text search reads every row of the advisor; HNSW and the iterative scan wait for a larger corpus, tested with the planner pushed; fusion ignores score gaps and has no relevance floor, so nonsense still gets records.
- **Watch:** a larger corpus (M4): a btree on `(advisor_id, mandate_id)`, HNSW for real, a `tsv` index if a leakproof route appears; German-to-English recall (PR 16's evals); how the model reads trust before M2 renders it.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 8 and defaults 10 and 13 (2026-11-08).
- `docs/reports/m0.md`, "M1 handovers": PR 5b sec-5, hnsw.iterative_scan and GIN index (2026-11-08).
- PR #24 (the recall check, the long query) and PR #27 (trust from the join, the sheets' index) handovers (2026-11-09).
- The measurements, on PostgreSQL 17.11 and pgvector 0.8.7, in PR #32 (2026-11-10).
- pgvector README, "Iterative Index Scans" (relaxed ordering, the materialized CTE, `+ 0` for PostgreSQL 17, `hnsw.max_scan_tuples`) and "Filtering", https://github.com/pgvector/pgvector (accessed 2026-11-10).
- PostgreSQL 17, "Row Security Policies" (policies run before the query's conditions, except leakproof functions), https://www.postgresql.org/docs/17/ddl-rowsecurity.html (accessed 2026-11-10).
- PostgreSQL 17, "CREATE FUNCTION", `LEAKPROOF` (superuser only), https://www.postgresql.org/docs/17/sql-createfunction.html (accessed 2026-11-10).
- Elastic, "Reciprocal rank fusion" (the formula, rank from 1, k = 60 by default, linking Cormack, Clarke and Büttcher's SIGIR 2009 paper), https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion (accessed 2026-11-10).
