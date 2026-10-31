# ADR 0005: Data language and the embedding model

## Status

Accepted (Timur, 2026-10-28, in the M0 plan).

## Date

2026-10-29

## Context

SITREP (situation report) retrieves from German and English documents; the synthetic corpus targets about 70 % German. The vector column needs a fixed dimension in M0 (PR 5b), long before the real embedder arrives in M1. The model file runs inside our process with client data, so where it comes from is a supply-chain question.

## Options

- **Language as `regconfig`.** The natural type. pg_upgrade refuses tables with `regconfig` columns, and Azure's in-place major upgrade uses pg_upgrade.
- **Language as `text` with a CHECK.** One more `CASE`, and upgradeable.
- **Embedders**, under three constraints: a licence we can use, no `trust_remote_code`, about 500 MB:
  - EmbeddingGemma: gated, Gemma terms.
  - Jina v3 and v5: CC-BY-NC.
  - gte-multilingual, Arctic-m-v2: remote code.
  - bge-m3, Qwen3-0.6B, e5-base: too large.
  - paraphrase-MiniLM: weak, 128 tokens.
  - quantized ONNX exports: tuned per instruction set.
  - `intfloat/multilingual-e5-small`: best German retrieval among the rest in the plan research (figure not re-verified; see Consequences).
  - `ibm-granite/granite-embedding-97m-multilingual-r2`: Apache-2.0, 384-d, a sigstore `model.sig`; BF16 weights, so `float32` must be pinned.
- **Runtime:** onnxruntime plus `tokenizers` (about 20 packages, about 150 MB) against torch (about 1 GB). fastembed cannot pin a revision: it always resolves the repository head.

## Decision

- **Language:** `lang text NOT NULL CHECK (lang IN ('german','english'))` and a STORED `tsv` through `CASE` over the two-argument `to_tsvector`. One query branch per language. One-argument text-search functions are banned (`tests/db/test_fts_language.py`).
- **Model:** e5-small at a pinned commit, 384 dimensions, `query: ` and `passage: ` prefixes added by the embedder, a 512-token cap with chunks of 400 tokens or fewer.
- **Licence:** MIT in the card metadata only; the repository has no LICENSE file.
- **Runtime:** onnxruntime plus `tokenizers`, with `onnx/model.onnx` (fp32, 470.3 MB) and `onnx/tokenizer.json` (17.1 MB).
- **Supply chain:** only the files in `FILES` are fetched, and each is checked against its sha256 before every load. `HF_HUB_OFFLINE=1` at runtime. Model id and revision are stored with every vector.
- **Anchor:** the model's `config.json` at the revision is committed as `app/config/embedding_model_config.json`; a test checks its sha256 and that `hidden_size` equals `DIM`.
- **Evals:** recordings compare vectors with a tolerance, not byte for byte.
- **CI in M1:** an anonymous `make model` step outside pytest, verified against `FILES`, cached by revision plus manifest hash. That `ci.yml` change needs Timur's yes.

```yaml
model_id: intfloat/multilingual-e5-small
revision: 614241f622f53c4eeff9890bdc4f31cfecc418b3
dim: 384
query_prefix: "query: "
passage_prefix: "passage: "
max_tokens: 512
files:
  config.json: 69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959
  onnx/model.onnx: ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665
  onnx/tokenizer.json: 0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39
```

## Consequences

- **Easier:** the vector column, the fake embedder (PR 6) and the real one agree from day one, and a changed model shows up as a failing test, not as silently worse retrieval.
- **Harder, recorded honestly:**
  - CPU speed is unmeasured; M1 benchmarks e5-small against granite-r2.
  - The German evidence is public MTEB, not banking text. The plan's German average (64.65 over 7 tasks) was not re-verified here: the leaderboard is an app we could not read.
  - e5's training data includes MS MARCO.
  - The ONNX sha256 values are git LFS oids from the Hugging Face API, not hashes of a download; `make model` checks the bytes in M1.
- **Watch:** a new revision means a new ADR-backed change to `FILES`, and re-embedding everything. The M1 loader rejects ONNX external-data paths and custom operator domains.

## Sources

- Hugging Face, `intfloat/multilingual-e5-small` model card at the revision, https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md (revision dated 2026-04-02; accessed 2026-10-29).
- Hugging Face API, file tree and LFS oids at the revision, https://huggingface.co/api/models/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx (queried 2026-10-29).
- Wang et al., "Multilingual E5 Text Embeddings: A Technical Report", arXiv:2402.05672, https://arxiv.org/abs/2402.05672 (2024-02-08).
- Hugging Face, `ibm-granite/granite-embedding-97m-multilingual-r2` at `835ad14087e140460703cf0fae09f97d469d65c2`, https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2 (revision dated 2026-05-18; accessed 2026-10-29).
- MTEB leaderboard, https://huggingface.co/spaces/mteb/leaderboard (accessed 2026-10-29).
- fastembed source, revision resolution, https://github.com/qdrant/fastembed/blob/f91d9168c0291daeafb0a54cebc7c5b4b83f312f/fastembed/text/onnx_embedding.py (accessed 2026-10-29).
- PostgreSQL 17, "pg_upgrade" (unsupported `reg*` types), https://www.postgresql.org/docs/17/pgupgrade.html (accessed 2026-10-29).
- Microsoft, "Major version upgrades" (uses pg_upgrade), https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-major-version-upgrade (page date 2026-09-10).
