# Runbook

Never log or paste `CROSSING_ED25519_SEED`, `CROSSING_KEY_PEPPER`, Stripe secrets, or provider tokens.

## Stuck `executing`

`executing` means Crossing already committed the dispatch barrier. Do **not** auto-refund.

1. `GET /v1` dashboard or query `invocations` for the id (status, tool, server, `idempotency_key`).
2. If the provider `supports_status_query`, operators can look up vendor status (Crossing `providers.status(server, invocation_id)`). Tokens stay in env.
3. Reconcile with evidence only: `POST /v1/invocations/{id}/reconcile` `outcome=committed|released` plus `evidence_ref` and `evidence_kind`. Released is forbidden if evidence says the vendor ran.
4. `mode=release` recovery on `executing` still refuses.

## Ambiguous

`reserved|executing → ambiguous` is CAS, no refund, claim stays. Treat as stuck executing: gather vendor evidence, then reconcile. Do not delete historical attempts.

## Outbox backlog

`GET /metrics` (`crossing_outbox_backlog`) or SQL `billing_outbox` where status in (`pending`,`failed`,`sending`).

- Worker: `python -m crossing.worker --once` then the loop.
- Stale `sending` (lease default 30s) is reclaimable by another drain.
- After 8 attempts the row is `dead` — inspect `last_error` (no secrets), fix Stripe config, do not rewrite ledger remaining.
- HTTP to Stripe only happens in the worker after COMMIT.

## Signing key

- Beta: `CROSSING_ED25519_SEED` (64 hex) + optional `CROSSING_KEY_ID` (`kid` on receipts).
- Production recommendation: HSM/KMS (`CROSSING_KEY_BACKEND=hsm` fails closed until a real `KeyStore` is wired).
- Prod refuses ephemeral keys unless `CROSSING_ALLOW_DEV=1`.
- Rotation: mint a new seed/kid, keep the old verify key available for historical receipts. Do not log seeds.

## Postgres backup / restore

Backup (example):

```bash
pg_dump -Fc "$DATABASE_URL_PQ" > crossing.dump
```

Restore onto empty cluster, then:

```bash
pg_restore --clean --if-exists -d "$DATABASE_URL_PQ" crossing.dump
alembic upgrade head
```

Do not `create_all` in production. Confirm `GET /readyz` after restore.
