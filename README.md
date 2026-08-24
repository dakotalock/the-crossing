# The Crossing

Agent Economic Runtime. Humans issue **signed spend mandates**. Agents and sub-agents may only spend what those mandates allow. Every `tools/call` is quoted, authorized, reserved, executed, then committed or released. Receipts are Ed25519-signed. Stripe is an outbox adapter, not the source of truth.

Default store is SQLite (`DATABASE_URL=sqlite:///./crossing.db`). Postgres is optional via `docker compose --profile postgres up` and the `psycopg[binary]` driver (see `requirements.txt`).

## Quick start

```bash
export CROSSING_ALLOW_DEV=1
pip install -r requirements.txt
PYTHONPATH=. python -m pytest tests -q
PYTHONPATH=. python -m crossing.demo
uvicorn crossing.api:app --reload
```

Production refuses a missing `CROSSING_ED25519_SEED` (64 hex chars) unless `CROSSING_ALLOW_DEV=1`.

HTTP routes authenticate with header `X-API-Key` matching `CROSSING_API_KEY` (prototype: one global key). Query-string `?key=` auth was removed so the secret does not leak into history, access logs, or `Referer`. When `CROSSING_ALLOW_DEV=1` and `CROSSING_API_KEY` is unset, the default test key is `dev` and a missing header is accepted on API routes except `GET /` (dashboard always requires `X-API-Key`). Unauthenticated minting is forbidden when `ALLOW_DEV` is off. This single shared key is prototype-only; marketplace-scoped credentials are not in this round.

## Architecture

```mermaid
flowchart LR
  Human[Principal / Human] -->|signed mandate| Crossing
  Agent[Agent / Child] -->|tools/call| Proxy
  Proxy[MCP Proxy] --> Policy
  Policy --> Ledger
  Ledger -->|reserve_and_commit| Budget[(remaining_cents)]
  Proxy --> MCP[Mock / upstream MCP]
  MCP -->|ok| Receipts
  Receipts -->|Ed25519| Human
  Ledger -->|commit + outbox row| Outbox
  Outbox -->|drain_outbox POST| Stripe
  MCP -->|error| Release[release reservation]
```

Lifecycle: **quote → authorize → reserve_and_commit → execute → commit | release**.

`reserve_and_commit()` treats **claim + reserve + invocation insert** as one savepoint. If the atomic debit fails (`BUDGET_EXCEEDED`), the LogicalOperation claim rolls back with it — the key is not left `in_progress` with no attempt. On success it **COMMITs before** the tool runs. A later transaction records success (ledger, receipt, billing_outbox pending) or release. Crash-after-execute-before-commit leaves a recoverable `reserved` ExecutionAttempt; it does not silently roll back the reserve.

**LogicalOperation vs ExecutionAttempt:** `IdempotencyRecord` is the logical operation (unique `(principal_id, idempotency_key)`). `Invocation` is an execution attempt; the unique index on `(principal_id, idempotency_key)` was dropped so a released attempt can be retried under the same key. Completed claims still replay. An `in_progress` claim still returns `IN_PROGRESS` (wait-or-conflict) unless it was rolled back or explicitly released for retry.

Budget debit is one conditional `UPDATE … RETURNING` on the mandate row (`remaining_cents >= cost AND revoked = 0`). That is atomic per row on SQLite and Postgres. Multi-host isolation is **not** fully solved: CI and default tests are still SQLite-only.

Recovery (`ledger.recover_reserved`): default `mode="ambiguous"` marks the attempt `ambiguous`, does **not** refund, and does **not** clear the LogicalOperation claim (retry stays blocked). Explicit `mode="release"` refunds remaining, marks the attempt released, and deletes the matching `IdempotencyRecord` so the same key can start a new attempt. Only use release when execute can be proven never to have started. The MCP execute-fail path already deletes an `in_progress` claim after release.

Child mandates are *attenuated*: child spend must be `> 0` and `<= parent remaining` and `<= parent spend_limit`; max_call, tools/servers subset, expiry not later than parent, `max_subagent_budget <= remaining`. Issuing a child **escrows** the child's spend cap from the parent remaining budget. Negative child spend is rejected and cannot mint parent budget.

Signed mandate payload contains immutable fields only (`spend_limit`, `max_call`, tools, servers, expiry, principal, agent, nonce, …). **`remaining_cents` and `calls_used` are mutable accounting and are not signed.** `remaining` starts equal to signed `spend_limit`. Before authorize/check_tool, columns are compared to the signed payload; mismatch is `SIGNED_STATE_DIVERGED`.

Root mandates are signed by The Crossing issuer key (`CROSSING_ED25519_SEED`). A caller-supplied signature is verified against the **registered** `principals.pubkey_hex` (or the issuer if none is registered). A pubkey bundled in the request is never a trust root unless it is already bound on the Principal.

## Threat model

In scope (enforced in-process):

| Threat | Control |
| --- | --- |
| Forged mandate / minted authority | Issuer key is the trust root; request pubkeys must already be bound |
| Tampered enforcement columns | Reconstruct signed payload; deny `SIGNED_STATE_DIVERGED` |
| Tampered receipt | Ed25519 over canonical receipt body (hashes by default) |
| Overspend / TOCTOU | Atomic per-row debit (`UPDATE … RETURNING`); durable reserve commit before execute; `BEGIN IMMEDIATE` on SQLite |
| Crash after execute | `invocations` attempt stays `reserved`; recover_reserved defaults to `ambiguous` (no refund, claim kept) |
| Child privilege escalation | Attenuation + agent descendant check |
| Negative child mint | Domain + API + SQLite CHECK `>= 0`; child spend `> 0` |
| Replay | `used_nonces` unique per principal; mandate nonce unique |
| Duplicate / confused charge | LogicalOperation claim insert (`in_progress`) in the same savepoint as reserve; `idempotency_key` + `request_hash`; conflict if hash differs; in-progress wait-or-conflict |
| Revoked operator | Agent + descendant revoke |
| Billing side effects | True outbox: Stripe only in `drain_outbox()` after COMMIT |
| Tool blast radius | Mandate tools/servers allow-lists; purchase ($5) denied when only search is granted |
| Unauthenticated mint | `X-API-Key` header only (no `?key=`); required when `CROSSING_ALLOW_DEV` is off; dashboard always requires the header |

Out of scope for this MVP:

- Multi-host consensus / serializable isolation across Postgres replicas (debit is atomic per mandate row; that is not the same as multi-host isolation)
- Hardware key custody (seed is an env var)
- Network-level MCP attestation of upstream servers
- Exactly-once execution at vendor / upstream MCP (Crossing forwards `invocation_id` / `idempotency_key` to the mock provider; it only guarantees at-most-one *dispatch from Crossing*)
- Marketplace-scoped API credentials (one global `CROSSING_API_KEY` is prototype-only)
- Real-money / production payment readiness
- Legal enforceability of a mandate in any jurisdiction

## HTTP

- `GET /health`
- `GET /` dashboard (plain HTML tables; `X-API-Key` header required, not `?key=`)
- `POST/GET /v1/principals`
- `POST/GET /v1/agents` and `POST /v1/agents/{id}/revoke`
- `POST /v1/mandates` `GET /v1/mandates/{id}`
- `POST /v1/invoke` (deny events are committed, then 403)
- `GET /v1/receipts` `GET /v1/receipts/{id}`

## Receipts

Default receipt body is hashes only (`request_hash`, `response_hash`) plus `agent_id`, `task_id`, `mandate_id`, `tool`, `server`, `amount_cents`, `outcome`, `reservation_id`. Full tool results are stored only when `CROSSING_RETAIN_PAYLOADS=1`. `task_id` is wired through ledger events and receipts.

## SDK

```python
from crossing.sdk import Crossing
cx = Crossing.in_process()
p = cx.create_principal("Alice")
a = cx.create_agent(p.id, "researcher")
m = cx.issue_mandate(p.id, a.id, 100, tools=["search"], servers=["mock"])
print(cx.quote("search"))  # 5 cents
r = cx.invoke(m.id, "search", {"q": "hi"}, idempotency_key="k1")
assert r.ok and cx.verify_receipt(r.receipt)
```

Crossing forwards a stable `invocation_id` and the `idempotency_key` into `mock_mcp.call_tool`. That is a hint for a provider that can honor it. Crossing still only guarantees at-most-one dispatch from Crossing, not exactly-once at the vendor.

## Pricing (mock MCP)

| Tool | Price |
| --- | --- |
| `search` | $0.05 |
| `purchase` / `expensive` | $5.00 |

Stripe: if `STRIPE_SECRET_KEY` is unset the adapter no-ops **and still writes a billing_outbox row** (`status=noop` after drain). HTTP to Stripe happens only in `drain_outbox()`, after the ledger/receipt commit. Failures mark `failed` (with `attempts`, `next_attempt_at`, `last_error`) and never undo `commit`. Drain claims rows by setting `status=sending` where status is pending/failed so two workers cannot double-send. Retryable rows are `pending` or (`failed` and `next_attempt_at <= now` and `attempts < 8`). Backoff is 1, 2, 4, 8… minutes (cap 60). After 8 attempts the row is `dead`.

## Postgres

Install `psycopg[binary]` (listed in `requirements.txt`) and set `DATABASE_URL=postgresql+psycopg://crossing:crossing@localhost:5432/crossing`.

The mandate debit is now an atomic conditional update per row (works on SQLite and Postgres). Default tests still run against SQLite. Do not treat multi-host races as solved until Postgres is in CI and the deployment uses a single primary with row-level updates.

## Review r4 (honest)

- Claim + reserve + invocation insert share one savepoint; budget deny does not poison the key as `in_progress`.
- `IdempotencyRecord` = LogicalOperation; `Invocation` = ExecutionAttempt (same key may have multiple attempts).
- `recover_reserved(mode="release")` refunds and clears the claim so the key is retryable. Default `ambiguous` does neither.
- Debit is atomic per mandate row. Not real-money-ready. Not exactly-once at vendors.
- Dashboard auth is `X-API-Key` only.
