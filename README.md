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

Production refuses a missing `CROSSING_ED25519_SEED` (64 hex chars) and a missing `CROSSING_KEY_PEPPER` unless `CROSSING_ALLOW_DEV=1`.

HTTP routes authenticate with header `X-API-Key` carrying a **tenant API key**. Secrets are shown once at creation; only `hmac-sha256(pepper, secret)` is stored. Visible prefix looks like `cxk_live_xxxx` / `cxk_test_xxxx` plus a public key id. Lookup is by prefix; compare is constant-time on hashes. Every resource query is filtered by the authenticated account/principal (wrong UUID → 404 closed). Dashboard accepts `X-API-Key` or the `crossing_session` cookie — never `?key=`. CORS defaults to deny (no `*`); set `CROSSING_CORS_ORIGINS` for explicit origins.

When `CROSSING_ALLOW_DEV=1`, boot mints a bootstrap **admin** key whose raw secret is `dev` (hash only stored). Production does not use a global `CROSSING_API_KEY`. Unauthenticated minting is forbidden.

Crossing budgets are **authorization limits, not custody**. There are no marketplace payouts. Charge customers for using the control plane.

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
  Outbox -->|worker drain_outbox POST| Stripe
  MCP -->|error| Release[release reservation]
```

Lifecycle: **quote → authorize → reserve_and_commit → mark_executing (COMMIT) → execute → finalize_success | (no auto-refund after dispatch)**.

`reserve_and_commit()` treats **claim + reserve + invocation insert** as one savepoint. If the atomic debit fails (`BUDGET_EXCEEDED`), the LogicalOperation claim rolls back with it — the key is not left `in_progress` with no attempt. On success it **COMMITs** `reserved`. Then `mark_executing` CAS `reserved → executing` and **COMMITs again before any bytes to the provider**. If that CAS loses, Crossing does not call the provider. `finalize_success` accepts `executing` (and `executed_ok` if that mid-state is used). `reserved` is not allowed: success requires the durable executing barrier. Those finalize functions own the **entire** terminal unit (reservation CAS, invocation status, receipt, billing outbox, LogicalOperation claim, ledger event) in one savepoint. Losing the reservation or invocation CAS rolls the savepoint back — no receipt-then-lost-commit, no refund-then-cleared-claim. Crash after execute but before finalize leaves an `executing` (or later `executed_fail`) attempt; it does not silently roll back the reserve.

**LogicalOperation vs ExecutionAttempt:** `IdempotencyRecord` is the logical operation (unique `(principal_id, idempotency_key)`). `Invocation` is an execution attempt; the unique index on `(principal_id, idempotency_key)` was dropped so a released attempt can be retried under the same key. Completed claims still replay. An `in_progress` claim still returns `IN_PROGRESS` (wait-or-conflict) unless it was rolled back or explicitly released for retry.

Scarce-authority transitions are conditional `UPDATE … RETURNING` on SQLite and Postgres: mandate debit (`remaining_cents >= cost AND revoked = 0 AND (max_calls IS NULL OR calls_used < max_calls)`), child-mandate escrow from parent remaining, and reservation `held → committed|released` CAS. CI runs a `sqlite` job and a `postgres` job. Multi-host isolation is still **not** fully solved (one primary, row-level updates — not consensus across replicas).

Recovery (`ledger.recover_reserved`): `reserved` means dispatch has not been marked started, so explicit `mode="release"` may refund via `finalize_release` (reservation `held→released` and invocation `reserved→released` must both win). `executing` means side-effect is uncertain: **never auto-refund**, **never clear** the LogicalOperation. `mode="release"` on `executing` refuses. Default `mode="ambiguous"` on `reserved` or `executing` CAS `UPDATE … WHERE status=reserved|executing` → `ambiguous` without refund (retry stays blocked). If the CAS loses, recovery refreshes and returns whatever won. Terminal `committed` / `released` / `ambiguous` / `executed_fail` is monotonic and is never overwritten. If the MCP call raises **after** `executing` was committed, Crossing records `executed_fail` and does **not** `finalize_release` (the vendor may have succeeded).

Child mandates are *attenuated*: child spend must be `> 0` and `<= parent remaining` and `<= parent spend_limit`; max_call, tools/servers subset, expiry not later than parent, `max_subagent_budget <= remaining`. Issuing a child **escrows** the child's spend cap from the parent remaining budget. Negative child spend is rejected and cannot mint parent budget.

Signed mandate payload contains immutable fields only (`spend_limit`, `max_call`, tools, servers, expiry, principal, agent, nonce, …). **`remaining_cents` and `calls_used` are mutable accounting and are not signed.** `remaining` starts equal to signed `spend_limit`. Before authorize/check_tool, columns are compared to the signed payload; mismatch is `SIGNED_STATE_DIVERGED`.

Root mandates are signed by The Crossing issuer key (`CROSSING_ED25519_SEED`). A caller-supplied signature is verified against the **registered** `principals.pubkey_hex` (or the issuer if none is registered). A pubkey bundled in the request is never a trust root unless it is already bound on the Principal.

## Threat model

In scope (enforced in-process):

| Threat | Control |
| --- | --- |
| Forged mandate / minted authority | Issuer key is the trust root; request pubkeys must already be bound |
| Tampered enforcement columns | Reconstruct signed payload; deny `SIGNED_STATE_DIVERGED` |
| Tampered / forged receipt | Ed25519 over canonical body; `verify_receipt` resolves `kid` from `/.well-known/crossing-keys` (or the process issuer key). A pubkey inside the receipt is never a trust root |
| Overspend / TOCTOU | Atomic per-row debit (`UPDATE … RETURNING`); durable reserve commit before execute; `BEGIN IMMEDIATE` on SQLite |
| Crash after execute | `invocations` attempt stays `executing` (dispatch started) or `executed_fail`; recover_reserved will not auto-refund `executing` |
| Child privilege escalation | Attenuation + agent descendant check |
| Negative child mint | Domain + API + SQLite CHECK `>= 0`; child spend `> 0` |
| Replay | `used_nonces` unique per principal; mandate nonce unique |
| Duplicate / confused charge | LogicalOperation claim insert (`in_progress`) in the same savepoint as reserve; `idempotency_key` + `request_hash`; conflict if hash differs; in-progress wait-or-conflict |
| Revoked operator | Agent + descendant revoke |
| Billing side effects | True outbox: Stripe only in `drain_outbox()` after COMMIT |
| Tool blast radius | Mandate tools/servers allow-lists; purchase ($5) denied when only search is granted |
| Unauthenticated mint | `X-API-Key` header only (no `?key=`); required when `CROSSING_ALLOW_DEV` is off; dashboard always requires the header |
| Provider SSRF | Operator `CROSSING_PROVIDER_URLS` allowlist; deny private/link-local/metadata; no customer URLs; no shell transport |

Out of scope for this MVP:

- Multi-host consensus / serializable isolation across Postgres replicas (debit is atomic per mandate row; that is not the same as multi-host isolation)
- Hardware key custody (seed is an env var)
- Network-level MCP attestation of upstream servers
- Exactly-once execution at vendor / upstream MCP (Crossing forwards `invocation_id` / `idempotency_key` to the mock provider; it only guarantees at-most-one *dispatch from Crossing*)
- Real-money / production payment readiness (this is still not D)
- Legal enforceability of a mandate in any jurisdiction

## HTTP

- `GET /health` `GET /healthz` (liveness)
- `GET /.well-known/crossing-keys` (issuer kid + pubkey; unauthenticated)
- `GET /readyz` (readiness; checks DB)
- `GET /metrics` (prometheus text; `?format=json` for counters: invokes, denials by reason, outbox backlog, `crossing_outbox_dead`)
- `GET /v1/account`
- `GET /` dashboard (plain HTML tables; `X-API-Key` header required, not `?key=`)
- `POST/GET /v1/principals`
- `POST/GET /v1/agents` and `POST /v1/agents/{id}/revoke`
- `POST /v1/mandates` `GET /v1/mandates/{id}`
- `POST /v1/invoke` (deny events are committed, then 403)
- `GET /v1/receipts` `GET /v1/receipts/{id}`
- `POST /v1/keys` `POST /v1/keys/{id}/rotate` `POST /v1/keys/{id}/revoke` (raw secret returned once)
- `POST /v1/mandates/{id}/revoke`
- `POST /v1/invocations/{id}/reconcile` (`outcome=committed|released`, `evidence_ref` required)
- `POST /v1/stripe/webhooks` (Stripe-Signature; replay of the same `event_id` is 200 no-op)
- `POST /v1/admin/accounts/{id}/stripe-customer` (admin; attach unique `stripe_customer_id`)
- `POST /v1/admin/outbox/{id}/requeue` (admin; dead → pending)
- `GET /v1/billing/status` (scope `billing:read`; plan id, customer present?, usage; never Stripe secrets)
- POST `/v1/principals` and `/v1/agents` require `write` or `mandate:issue` (not `read` alone)

## Receipts

`verify_receipt` trusts `kid` only if it appears in that directory (or the in-process issuer key). A `pubkey_hex` riding on the receipt is not a trust root.

Default receipt body is hashes only (`request_hash`, `response_hash`) plus `agent_id`, `task_id`, `mandate_id`, `tool`, `server`, `amount_cents`, `outcome`, `reservation_id`, and `kid` (signing key version). Full tool results are stored only when `CROSSING_RETAIN_PAYLOADS=1`. `task_id` is wired through ledger events and receipts. Production signing still refuses ephemeral keys unless `CROSSING_ALLOW_DEV=1`. Default store is env seed; production recommendation is HSM/KMS (`CROSSING_KEY_BACKEND=hsm` fails closed until wired).

## SDK

In-process and HTTP `/v1` (`crossing.sdk.CrossingClient`): authenticate, account, create agent, issue mandate, invoke with idempotency key, get/verify receipt, remaining budget, billing status. Five-minute path: `docs/quickstart.md`.

```python
from crossing.sdk import Crossing
cx = Crossing.in_process()
p = cx.create_principal("Alice")
a = cx.create_agent(p.id, "researcher")
m = cx.issue_mandate(p.id, a.id, 100, tools=["search"], servers=["mock"])
print(cx.quote("search"))  # 5 cents
r = cx.invoke(m.id, "search", {"q": "hi"}, idempotency_key="k1")
assert r.ok and cx.verify_receipt(r.receipt)
assert r.receipt["body"]["kid"]
```

## Providers

`mock` stays in-process for tests (`crossing.mock_mcp`). Other servers must be named in operator env `CROSSING_PROVIDER_URLS` (never a customer-supplied URL). Transport is MCP HTTP JSON-RPC with `invocation_id` + `idempotency_key` forwarded, timeouts, and capability flags (`supports_idempotency`, `supports_status_query`). SSRF: loopback, RFC1918, link-local, and cloud metadata are denied unless `CROSSING_PROVIDER_INTERNAL_ALLOWLIST=1` *and* the URL is still allowlisted. Provider tokens: `CROSSING_PROVIDER_TOKEN_<NAME>` from env, never receipts or logs. Customers cannot configure a server-side shell.

Crossing still only guarantees at-most-one dispatch from Crossing, not exactly-once at the vendor.

## Pricing (mock MCP)

| Tool | Price |
| --- | --- |
| `search` | $0.05 |
| `purchase` / `expensive` | $5.00 |

Stripe is **control-plane billing, not custody**. Map `accounts.stripe_customer_id` (nullable **unique**). There is no global `STRIPE_CUSTOMER_ID` env fallback — only the account row. Env: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` (subscription), optional `STRIPE_METER_EVENT`. If `STRIPE_SECRET_KEY` is unset the adapter no-ops **and still writes a billing_outbox row** (`status=noop` after the worker drains). When the secret **is** set, `invoke` and `reconcile_commit` refuse with `BILLING_REQUIRED` unless the authenticated account has a customer id and `stripe_status` is not unpaid/canceled.

HTTP to Stripe happens **only** in `python -m crossing.worker` (`drain_outbox`): CAS pending/failed/stale-sending → `sending` **COMMIT**, then POST, then CAS `sending→sent|failed|dead` **COMMIT**. The API process does not drain. Failures never undo ledger `commit`. One outbox row per `receipt_id` (unique). Stripe Idempotency-Key is `receipt_id`. Backoff is 1, 2, 4, 8… minutes (cap 60). After 8 attempts the row is `dead` (visible as `crossing_outbox_dead`). Requeue: `POST /v1/admin/outbox/{id}/requeue` or `python -m crossing.worker --requeue-dead [id]`.

Platform fee: integer `CROSSING_FEE_BPS` (default **0**). Do not turn fees on until a Stripe invoice/line item exists — `post_stripe` meters usage only and does **not** send a fee. Microcents accumulate (`amount_cents * bps * 100`); `fee_invoiced_cents` is not incremented (Crossing does not pretend Stripe was billed). Commercial Stripe price ids are not written to the ledger.

Webhook: verify `stripe-signature`, persist `stripe_events.event_id` as PK. Replay is 200 + `duplicate: true` and does not mutate Crossing remaining/commit state.

## Postgres

Install `psycopg[binary]` (listed in `requirements.txt`) and set `DATABASE_URL` (compose binds Postgres at `127.0.0.1:5432` only; password from `POSTGRES_PASSWORD`, local default is not a production secret).

The mandate debit, child escrow, and reservation terminal transitions are conditional updates (SQLite and Postgres). Local default is still SQLite. CI has a `postgres` job (`DATABASE_URL=postgresql+psycopg://…`). That is not multi-host consensus.

## Alembic

`alembic.ini` + `alembic/versions/` ship migrations matching current models (including `accounts`, `api_keys`, `reconciliation_events`, `stripe_events`).

- Dev (`CROSSING_ALLOW_DEV=1`): sqlite may `create_all` **or** `alembic upgrade head`. `create_all` runs only when required tables are missing (so an Alembic-applied postgres DB is not rewritten).
- Production (`CROSSING_ALLOW_DEV!=1`): **do not** `create_all`. Apply migrations before boot (`alembic upgrade head`). API/worker refuse to start if required tables are missing.

CI: `sqlite` job (`CROSSING_ALLOW_DEV=1`, `CROSSING_KEY_PEPPER` set) and `postgres` job (empty DB → `alembic upgrade head` → pytest, no `create_all`). Sqlite job also runs `ruff check`, `docker build` (no registry push), and `pip-audit` when that extra installs; if `pip-audit` cannot install, CI continues and this note is the skip record.

Deploy files: `Dockerfile` (API), `Dockerfile.worker` (same layout, worker CMD), `docker-compose.yml` (postgres / api / worker / optional caddy), `Caddyfile`, `.env.example` (names only). Ops: `docs/deploy.md`, `docs/runbook.md`. JSON logs (stdlib) carry `request_id` / `invocation_id` and not payloads. Rate limit is in-memory per API key (single instance; not multi-host).

`alembic/env.py` reads `DATABASE_URL` the same way as `crossing/db.py`.

## Reconciliation

Operator CAS (never overwrite committed/released; lost CAS is a no-op returning current):

```
UPDATE invocations SET status='reconciled_committed'
 WHERE id=:id AND status IN ('ambiguous','executed_fail') RETURNING …
UPDATE invocations SET status='reconciled_released'
 WHERE id=:id AND status IN ('ambiguous','executed_fail') RETURNING …
```

`reconciled_committed` requires `evidence_kind=did_execute` and applies the same economic effects as `finalize_success` (receipt + outbox + claim completed) **exactly once**; a prior success is not double-billed. `reconciled_released` requires `did_not_execute` and refunds remaining exactly once (`held→released` CAS). LogicalOperation is cleared only when evidence says execution **did not** occur. `evidence_ref` is required; actor is the API key id. Historical attempts are never deleted. `finalize_release` defaults to invocation status `reserved` only (does not refund `executed_fail`).

Provider exactly-once is **not** guaranteed without reconcil evidence. Crossing still only guarantees at-most-one *dispatch from Crossing*.

## Review r8 (honest)

- Transitions **to** `ambiguous` are CAS (`reserved→ambiguous` or `executing→ambiguous`). A lost CAS refreshes and returns the winner; recovery does not refund or clear the claim on the ambiguous path.
- Terminal invocation states (`committed`, `released`, `executed_fail`, `ambiguous`) are monotonic: `recover_reserved` and `mark_executing` (still only `reserved→executing`) will not overwrite them.
- `finalize_success` requires the durable executing barrier (`executing`, or `executed_ok` if used). Calling it on `reserved` returns `won=False` with no receipt and no reservation commit.
- Tenant-scoped hashed API keys (not a global `CROSSING_API_KEY`). Still not a marketplace.
- Still cannot prove vendor exactly-once without provider idempotency / reconcil evidence. Crossing only guarantees at-most-one *dispatch from Crossing*.
- Still not real-money-ready. Still not D. Still no custody.

## Review r7 (kept)

- `executing` is CAS + COMMIT **before** provider I/O. Recovery will not auto-refund `executing`. A provider exception after dispatch is `executed_fail` (no refund, claim stays). `reserved` (never dispatched) is still releasable.
- Still cannot prove vendor exactly-once without provider idempotency. Crossing only guarantees at-most-one *dispatch from Crossing*.
- Tenant hashed keys; still not a marketplace.
- Still not real-money-ready.

## Review r6 (kept)

- `finalize_success` / `finalize_release` own the whole ending: reservation CAS, invocation, receipt, outbox, claim, and ledger event share one savepoint. A lost CAS does nothing else. `commit()` / `release()` remain low-level reservation helpers.
- Tenant hashed keys; still not a marketplace.
- Still at-most-one Crossing dispatch, not provider exactly-once.
- Still not real-money-ready.

## Review r5 (kept)

- Scarce-authority transitions are conditional `UPDATE`s (debit + `max_calls`, child escrow, reservation CAS). Failed debit distinguishes `BUDGET_EXCEEDED` vs `MAX_CALLS_EXCEEDED`; PolicyDenied and the deny ledger note match.
- CI has a `sqlite` job and a `postgres` job. Local pytest still defaults to a temp SQLite file unless `DATABASE_URL` is postgres.

## Review r4 (kept)

- Claim + reserve + invocation insert share one savepoint; budget deny does not poison the key as `in_progress`.
- `IdempotencyRecord` = LogicalOperation; `Invocation` = ExecutionAttempt (same key may have multiple attempts).
- `recover_reserved(mode="release")` refunds and clears the claim so the key is retryable. Default `ambiguous` does neither.
- Dashboard auth is `X-API-Key` only.
