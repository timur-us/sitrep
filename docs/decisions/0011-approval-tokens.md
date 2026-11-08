# ADR 0011: Approval tokens

## Status

Accepted in outline (Timur, 2026-11-08: the M1 plan, PR 2); this text, and its additions listed in PR 2, await Fable's design read. Built in PR 14.

## Date

2026-11-08

## Context

SITREP (situation report) runs `draft_email` and `create_followup` only after the advisor approves them in the UI, with a server-issued single-use token (brief; threat-model path 4). ADR 0001's amendment left the design open. No new persistent secret beside the API key (threat model, "Assets"). The model must never mint, see or replay a token.

## Options

- **No token, a state flag:** any path that reaches the runner, a loop bug included, runs an approved call without the advisor's step.
- **Approve runs the call itself:** one step fewer, but the brief and the API contract ask for a token.
- **An HMAC-signed token:** a key to keep, and single use needs stored state anyway.
- **A JWT:** a library, algorithm confusion, and the same state.
- **An opaque random token stored as its hash:** no key; a database read yields only hashes.

## Decision

The last option, in `app/harness/approval.py` (PR 14):
- **`propose()`** stores under row-level security the advisor, the mandate (the composite foreign key `(advisor_id, mandate_id)`, so a foreign mandate stores nothing), the tool, and the canonical call `{tool, mandate_id, arguments}` as text (sorted-key JSON, not `jsonb`) with its sha256. The proposal id is random and approves nothing.
- **`approve(advisor, proposal_id)`** is reachable from the advisor's UI session only: the login and, as for redeem, an exact Origin (a missing one refused) whatever the login; a scan keeps tools, the loop and the runners from it. It moves `pending` to `approved` once, draws `secrets.token_urlsafe(32)`, returns it with its expiry once, and stores only the token's sha256, bound to the call's sha256, expiring after 10 minutes on the database's clock.
- **`redeem(advisor, proposal_id, token)`** refuses a token of any other shape, then runs one `UPDATE … RETURNING` in the advisor transaction, moving `approved` to `done` if the hash is equal and unexpired. Read Committed re-checks that after a concurrent redeem commits, so one wins. Redeem recomputes sha256 over the returned text, which must equal the bound hash, with tool and mandate equal to the row's; only that text runs, validated again by the registry. The handler writes only in this transaction, so a failure rolls both back and the call succeeds at most once.
- **No signing key.** Every refusal of approve or redeem is the same 409; the statement does not say which condition failed. The token travels in a request body, never a URL, and is never logged, traced or shown to the model.
- The Protocol's `approve` and `redeem` signatures change in PR 14.

```yaml
token_bytes: 32
hash: sha256
ttl_seconds: 600
single_use: true
binding: [advisor_id, proposal_id, tool, mandate_id, call_sha256]
```

## Consequences

- **Easier:** no key to keep; a leaked database dump replays nothing.
- **Harder:**
  - The token proves the advisor's step, not intent beyond the arguments shown: the panel (PR 15) is the real control. The gate is as strong as approve()'s unreachability, which a name scan checks.
  - It holds against the model and the loop, not against SQL injection as the app role, which may update proposal state or write drafts directly, nor against a superuser (ADR 0008).
  - An unredeemed approval expires; the advisor asks again for a new proposal.
  - A non-constant-time comparison leaks bits of a sha256, not of the token.
- **Watch:** an effect outside the database (sending, M4) needs an outbox keyed to the proposal. M2's recipient allowlist runs at redeem too. Path 4 still says "signed"; PR 14 amends it.

## Sources

- `docs/plans/m1-mvp-core.md`, PR 2 and PR 14, the API contract (2026-11-08).
- ADR 0001, "Amendment (PR 6, 2026-11-03)", and `app/harness/approval.py` at `f866a58` (2026-11-08).
- `docs/threat-model.md`, "Assets" and path 4 (2026-11-08).
- Python 3.12, `secrets` ("32 bytes (256 bits) of randomness is sufficient", as of 2015), https://docs.python.org/3.12/library/secrets.html (accessed 2026-11-08).
- PostgreSQL 17, "Transaction Isolation", Read Committed (an UPDATE re-checks its WHERE clause on the updated row version after a concurrent commit), https://www.postgresql.org/docs/17/transaction-iso.html (accessed 2026-11-08).
- RFC 8725, "JSON Web Token Best Current Practices", section 2.1, "Weak Signatures and Insufficient Signature Validation" (`none`, RS256 switched to HS256), https://www.rfc-editor.org/rfc/rfc8725 (2020; accessed 2026-11-08).
