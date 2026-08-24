# Deploy

Topology: **Caddy → FastAPI → Postgres + worker**.

```
Internet → Caddy (TLS) → uvicorn crossing.api:app
                         ↘ python -m crossing.worker  (outbox drain)
                         ↘ Postgres
```

No live VM is required for this repo. Do not put secrets in git.

## Dockerfile

`Dockerfile` is the API image. The worker uses the **same image** with a different `CMD` (`python -m crossing.worker`). See `docker-compose.yml`.

```bash
docker build -t crossing:local .
```

Do not push a registry from CI.

## Compose (staging)

`docker compose up` starts **postgres + api + worker**. The worker **must run**; without it, billing outbox rows stay `pending`/`dead` and never drain to Stripe. Profile `caddy` adds the reverse proxy.

```bash
cp .env.example .env   # fill values locally; never commit .env
docker compose up --build
# optional TLS proxy:
# docker compose --profile caddy up --build
```

Equivalent full stack: `docker compose --profile staging up --build` (caddy is the only profiled service; api/worker/postgres start by default).

## Migration

Production must not `create_all`. Apply schema before boot:

```bash
alembic upgrade head
```

API/worker refuse to start if required tables are missing (`CROSSING_ALLOW_DEV!=1`).

## Environment

Names only — see `.env.example`. Required in production:

- `DATABASE_URL`
- `CROSSING_ED25519_SEED`
- `CROSSING_KEY_PEPPER`
- `CROSSING_PROVIDER_URLS` (operator allowlist; never customer-supplied URLs)

Provider tokens: `CROSSING_PROVIDER_TOKEN_<NAME>`.

## Backup

See `docs/runbook.md`. Schedule `pg_dump` and store off-box. Test restore on a scratch database.

Compose binds Postgres to `127.0.0.1:5432` only (not `0.0.0.0`). Set `POSTGRES_PASSWORD` in `.env`; do not ship a production password in compose. Caddyfile `:80` is local/staging — TLS is required on the internet (hostname site block).

## Firewall

- Postgres not on the public internet (localhost bind or no host port).
- API only via Caddy (HTTPS / TLS at the edge).
- Egress: Stripe + allowlisted provider hosts only.
- Deny cloud metadata (`169.254.169.254`) from app units (Crossing also refuses those URLs unless internal allowlist).

## Request limits / shutdown

- `CROSSING_MAX_BODY_BYTES` (default 65536).
- `CROSSING_PROVIDER_TIMEOUT_SECONDS` (default 15).
- Uvicorn `--timeout-graceful-shutdown 30`: finish in-flight HTTP; worker finishes the current drain batch then exits on SIGTERM.

## Checklist

- [ ] Secrets in the host secret store, not the image
- [ ] `alembic upgrade head`
- [ ] `GET /healthz` and `GET /readyz`
- [ ] `CROSSING_ALLOW_DEV` unset in prod
- [ ] Provider URLs allowlisted
- [ ] Stripe webhook endpoint with `STRIPE_WEBHOOK_SECRET`
- [ ] Worker running
- [ ] Postgres backups

## Rollback

1. Stop api+worker.
2. Restore DB from the pre-change dump if the migration is not backward compatible.
3. Deploy the previous image tag.
4. If the migration *is* additive, rolling the image back without restore is enough.
5. Confirm `/readyz` and a single `--once` worker drain.

Rate limit is **in-memory / single instance**. Multiple API replicas do not share the limiter.
