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

HTTP mutating routes require `X-API-Key` matching `CROSSING_API_KEY`. When `CROSSING_ALLOW_DEV=1` and `CROSSING_API_KEY` is unset, the default test key is `dev` and a missing header is accepted on API routes. `GET /` (dashboard) always requires the same key (header `X-API-Key` or query `?key=`); unauthenticated dashboard requests return 401. Unauthenticated minting is forbidden when `ALLOW_DEV` is off.

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

`reserve_and_commit()` writes the reservation, decrements `remaining_cents`, inserts an `invocations` row (`reserved`), and **COMMITs before** the tool runs. A later transaction records success (ledger, receipt, billing_outbox pending) or release. Crash-after-execute-before-commit leaves a recoverable `reserved` row; it does not silently roll back the reserve.

Recovery (`ledger.recover_reserved`): default `mode="ambiguous"`. A `reserved` row with no outcome is marked `ambiguous` and remaining is **not** refunded. Operators inspect ambiguous invocations. Only explicit `mode="release"` refunds, and only when execute can be proven never to have started.

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
| Overspend / TOCTOU | Durable reserve commit before execute; `BEGIN IMMEDIATE` |
| Crash after execute | `invocations` row stays `reserved`; recover_reserved defaults to `ambiguous` (no refund) |
| Child privilege escalation | Attenuation + agent descendant check |
| Negative child mint | Domain + API + SQLite CHECK `>= 0`; child spend `> 0` |
| Replay | `used_nonces` unique per principal; mandate nonce unique |
| Duplicate / confused charge | Unique claim insert (`in_progress`) before reserve; `idempotency_key` + `request_hash`; conflict if hash differs; in-progress wait-or-conflict |
| Revoked operator | Agent + descendant revoke |
| Billing side effects | True outbox: Stripe only in `drain_outbox()` after COMMIT |
| Tool blast radius | Mandate tools/servers allow-lists; purchase ($5) denied when only search is granted |
| Unauthenticated mint | `X-API-Key` required when `CROSSING_ALLOW_DEV` is off; dashboard always requires the key |

Out of scope for this MVP:

- Multi-host consensus / serializable isolation across Postgres replicas
- Hardware key custody (seed is an env var)
- Network-level MCP attestation of upstream servers
- Legal enforceability of a mandate in any jurisdiction

## HTTP

- `GET /health`
- `GET /` dashboard (plain HTML tables; `X-API-Key` or `?key=` required)
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

## Pricing (mock MCP)

| Tool | Price |
| --- | --- |
| `search` | $0.05 |
| `purchase` / `expensive` | $5.00 |

Stripe: if `STRIPE_SECRET_KEY` is unset the adapter no-ops **and still writes a billing_outbox row** (`status=noop` after drain). HTTP to Stripe happens only in `drain_outbox()`, after the ledger/receipt commit. Failures mark `failed` (with `attempts`, `next_attempt_at`, `last_error`) and never undo `commit`. Drain claims rows by setting `status=sending` where status is pending/failed so two workers cannot double-send. Retryable rows are `pending` or (`failed` and `next_attempt_at <= now` and `attempts < 8`). Backoff is 1, 2, 4, 8… minutes (cap 60). After 8 attempts the row is `dead`.

## Postgres

Install `psycopg[binary]` (listed in `requirements.txt`) and set `DATABASE_URL=postgresql+psycopg://crossing:crossing@localhost:5432/crossing`.
