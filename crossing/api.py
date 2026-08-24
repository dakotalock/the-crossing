from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from crossing import abuse, auth, crypto, db, ledger, logging_json, metrics
from crossing.auth import AuthContext, SESSION_COOKIE
from crossing.dashboard import landing, render
from crossing.identity import create_agent, create_principal, revoke_agent
from crossing.lifecycle import invoke
from crossing.mandate import issue_mandate, revoke_mandate
from crossing.models import Account, Agent, ApiKey, Mandate, Principal, Receipt
from crossing.policy import PolicyDenied, Reason
from crossing.receipts import to_dict, verify_receipt

log = logging.getLogger("crossing.api")


def _cors_origins() -> list[str]:
    raw = os.environ.get("CROSSING_CORS_ORIGINS") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _boot() -> None:
    if os.environ.get("CROSSING_ALLOW_DEV") != "1":
        crypto.require_production_secrets()
        auth.key_pepper()
    if db.SessionLocal is None:
        db.init_db()
    from crossing import billing

    billing.warn_unsent_fees()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging_json.configure_logging()
    _boot()
    yield
    log.info("shutdown")


app = FastAPI(title="The Crossing", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type", "X-Request-Id"],
)


class BodyLimitMiddleware:
    """Reject oversized request bodies via Content-Length (beta)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        cl = headers.get("content-length")
        limit = abuse.max_body_bytes()
        if cl:
            try:
                n = int(cl)
            except ValueError:
                n = 0
            if n > limit:
                resp = Response(
                    content=json.dumps({"detail": {"reason": Reason.PAYLOAD_TOO_LARGE}}),
                    status_code=413,
                    media_type="application/json",
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or str(uuid.uuid4())
    token = logging_json.request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        logging_json.request_id_var.reset(token)
    response.headers["X-Request-Id"] = rid
    return response


app.add_middleware(BodyLimitMiddleware)


def _unauth(detail: str = "unauthenticated") -> HTTPException:
    return HTTPException(status_code=401, detail={"reason": Reason.UNAUTHORIZED, "detail": detail})


def _closed() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


def _forbidden(detail: str = "forbidden") -> HTTPException:
    return HTTPException(status_code=403, detail={"reason": Reason.UNAUTHORIZED, "detail": detail})


def authenticate_token(presented: str | None) -> AuthContext:
    if not presented:
        raise _unauth()
    with db.session_scope() as s:
        ctx = auth.authenticate(s, presented)
        if ctx is None:
            raise _unauth("unauthenticated")
        return ctx


def require_auth(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> AuthContext:
    _boot()
    return authenticate_token(x_api_key)


def require_scope(scope: str):
    def _inner(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
        if not ctx.has_scope(scope):
            raise _forbidden(f"missing scope {scope}")
        return ctx

    return _inner


def require_write(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
    if ctx.has_scope("write") or ctx.has_scope("mandate:issue"):
        return ctx
    raise _forbidden("missing write scope")


def _own_principal(session, ctx: AuthContext, principal_id: str) -> Principal:
    p = session.get(Principal, principal_id)
    if p is None:
        raise _closed()
    if ctx.is_admin:
        return p
    if p.account_id != ctx.account_id:
        raise _closed()
    return p


def _own_mandate(session, ctx: AuthContext, mandate_id: str) -> Mandate:
    m = session.get(Mandate, mandate_id)
    if m is None:
        raise _closed()
    if ctx.is_admin:
        return m
    principal_ids = _principal_ids(session, ctx)
    if m.principal_id not in principal_ids:
        raise _closed()
    return m


def _principal_ids(session, ctx: AuthContext) -> set[str]:
    rows = session.query(Principal).filter(Principal.account_id == ctx.account_id).all()
    return {r.id for r in rows}


def _filter_principals(session, ctx: AuthContext):
    q = session.query(Principal)
    if not ctx.is_admin:
        q = q.filter(Principal.account_id == ctx.account_id)
    return q


class PrincipalIn(BaseModel):
    name: str
    pubkey_hex: str | None = None


class AgentIn(BaseModel):
    principal_id: str
    name: str
    parent_id: str | None = None


class MandateIn(BaseModel):
    principal_id: str
    agent_id: str
    spend_limit_cents: int = Field(ge=0)
    max_call_cents: int | None = Field(default=None, ge=0)
    max_calls: int | None = 1000
    tools: list[str] | None = None
    servers: list[str] | None = None
    expires_at: datetime | None = None
    max_subagent_budget_cents: int | None = Field(default=None, ge=0)
    parent_mandate_id: str | None = None
    nonce: str | None = None
    signature: str | None = None
    pubkey_hex: str | None = None


class InvokeIn(BaseModel):
    mandate_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    server: str = "mock"
    idempotency_key: str | None = None
    nonce: str | None = None
    task_id: str | None = None


class KeyIn(BaseModel):
    account_id: str | None = None
    kind: str = "customer"
    scopes: list[str] | None = None
    expires_at: datetime | None = None


class ReconcileIn(BaseModel):
    outcome: str  # committed | released
    evidence_ref: str
    evidence_kind: str


class StripeCustomerIn(BaseModel):
    stripe_customer_id: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    _boot()
    try:
        with db.session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@app.get("/metrics")
def get_metrics(request: Request) -> Response:
    accept = (request.headers.get("accept") or "") + " " + (request.query_params.get("format") or "")
    if "json" in accept.lower():
        return JSONResponse(metrics.snapshot())
    return PlainTextResponse(metrics.prometheus_text(), media_type="text/plain; version=0.0.4")


@app.get("/", response_class=HTMLResponse)
def public_landing() -> str:
    return landing()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    crossing_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    _boot()
    token = x_api_key or crossing_session
    if not token:
        raise _unauth()
    ctx = authenticate_token(token)
    if not ctx.has_scope("read"):
        raise _forbidden("missing scope read")
    with db.session_scope() as s:
        return render(s, account_id=None if ctx.is_admin else ctx.account_id, is_admin=ctx.is_admin)


@app.get("/v1/account")
def get_account(ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
    with db.session_scope() as s:
        acct = s.get(Account, ctx.account_id)
        if acct is None:
            raise _closed()
        return {
            "account_id": acct.id,
            "name": acct.name,
            "api_key_id": ctx.api_key_id,
            "prefix": ctx.prefix,
            "kind": ctx.kind,
            "scopes": ctx.scopes,
            "stripe_customer_present": bool(acct.stripe_customer_id),
        }


@app.post("/v1/principals")
def post_principal(body: PrincipalIn, ctx: AuthContext = Depends(require_write)) -> dict[str, Any]:
    with db.session_scope() as s:
        if not ctx.is_admin:
            existing = s.query(Principal).filter(Principal.account_id == ctx.account_id).first()
            if existing is not None:
                return {"id": existing.id, "name": existing.name, "pubkey_hex": existing.pubkey_hex}
            p = create_principal(s, body.name, pubkey_hex=body.pubkey_hex, account_id=ctx.account_id)
            return {"id": p.id, "name": p.name, "pubkey_hex": p.pubkey_hex}
        p = create_principal(s, body.name, pubkey_hex=body.pubkey_hex)
        return {"id": p.id, "name": p.name, "pubkey_hex": p.pubkey_hex}


@app.get("/v1/principals")
def list_principals(ctx: AuthContext = Depends(require_scope("read"))) -> list[dict[str, Any]]:
    with db.session_scope() as s:
        rows = _filter_principals(s, ctx).all()
        return [{"id": p.id, "name": p.name, "pubkey_hex": p.pubkey_hex} for p in rows]


@app.post("/v1/agents")
def post_agent(body: AgentIn, ctx: AuthContext = Depends(require_write)) -> dict[str, Any]:
    try:
        with db.session_scope() as s:
            _own_principal(s, ctx, body.principal_id)
            a = create_agent(s, body.principal_id, body.name, parent_id=body.parent_id)
            return {
                "id": a.id,
                "principal_id": a.principal_id,
                "parent_id": a.parent_id,
                "name": a.name,
                "revoked": a.revoked,
            }
    except PolicyDenied as exc:
        raise HTTPException(status_code=400, detail={"reason": exc.reason, "detail": exc.detail}) from exc


@app.post("/v1/agents/{agent_id}/revoke")
def post_revoke(agent_id: str, ctx: AuthContext = Depends(require_write)) -> dict[str, Any]:
    with db.session_scope() as s:
        a = s.get(Agent, agent_id)
        if a is None:
            raise _closed()
        _own_principal(s, ctx, a.principal_id)
        a = revoke_agent(s, agent_id)
        return {"id": a.id, "revoked": a.revoked}


@app.get("/v1/agents")
def list_agents(ctx: AuthContext = Depends(require_scope("read"))) -> list[dict[str, Any]]:
    with db.session_scope() as s:
        q = s.query(Agent)
        if not ctx.is_admin:
            ids = _principal_ids(s, ctx)
            q = q.filter(Agent.principal_id.in_(ids))
        return [
            {"id": a.id, "principal_id": a.principal_id, "parent_id": a.parent_id, "name": a.name, "revoked": a.revoked}
            for a in q.all()
        ]


@app.post("/v1/mandates")
def post_mandate(body: MandateIn, ctx: AuthContext = Depends(require_scope("mandate:issue"))) -> dict[str, Any]:
    try:
        with db.session_scope() as s:
            _own_principal(s, ctx, body.principal_id)
            m = issue_mandate(s, **body.model_dump())
            return {
                "id": m.id,
                "remaining_cents": m.remaining_cents,
                "spend_limit_cents": m.spend_limit_cents,
                "signature": m.signature,
                "nonce": m.nonce,
            }
    except PolicyDenied as exc:
        raise HTTPException(status_code=400, detail={"reason": exc.reason, "detail": exc.detail}) from exc


@app.get("/v1/mandates/{mandate_id}")
def get_mandate(mandate_id: str, ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
    with db.session_scope() as s:
        m = _own_mandate(s, ctx, mandate_id)
        return {
            "id": m.id,
            "remaining_cents": m.remaining_cents,
            "spend_limit_cents": m.spend_limit_cents,
            "tools": m.tools_list(),
            "revoked": m.revoked,
        }


@app.post("/v1/mandates/{mandate_id}/revoke")
def post_mandate_revoke(mandate_id: str, ctx: AuthContext = Depends(require_scope("mandate:revoke"))) -> dict[str, Any]:
    with db.session_scope() as s:
        _own_mandate(s, ctx, mandate_id)
        m = revoke_mandate(s, mandate_id)
        return {"id": m.id, "revoked": m.revoked}


@app.post("/v1/invoke")
def post_invoke(body: InvokeIn, ctx: AuthContext = Depends(require_scope("invoke"))) -> dict[str, Any]:
    denied = None
    try:
        abuse.check_rate_limit(ctx.api_key_id)
    except PolicyDenied as exc:
        metrics.inc_deny(exc.reason)
        raise HTTPException(status_code=429, detail={"reason": exc.reason, "detail": exc.detail}) from exc
    try:
        with db.session_scope() as s:
            _own_mandate(s, ctx, body.mandate_id)
            try:
                result = invoke(s, **body.model_dump())
            except PolicyDenied as exc:
                from crossing.models import Mandate as MandateModel

                m = s.get(MandateModel, body.mandate_id)
                if m is not None:
                    ledger.append_event(
                        s,
                        principal_id=m.principal_id,
                        mandate_id=m.id,
                        kind="deny",
                        note=exc.reason,
                        task_id=body.task_id,
                    )
                denied = {"ok": False, "reason": exc.reason, "detail": exc.detail}
            else:
                if not result.ok:
                    denied = result.to_dict()
                else:
                    return result.to_dict()
    except HTTPException:
        raise
    except PolicyDenied as exc:
        with db.session_scope() as s:
            from crossing.models import Mandate as MandateModel

            m = s.get(MandateModel, body.mandate_id)
            if m is not None:
                try:
                    _own_mandate(s, ctx, body.mandate_id)
                except HTTPException:
                    raise _closed() from exc
                ledger.append_event(
                    s,
                    principal_id=m.principal_id,
                    mandate_id=m.id,
                    kind="deny",
                    note=exc.reason,
                    task_id=body.task_id,
                )
        raise HTTPException(status_code=403, detail={"reason": exc.reason, "detail": exc.detail}) from exc
    if denied:
        raise HTTPException(status_code=403, detail=denied)
    raise HTTPException(status_code=500, detail="invoke produced no result")


@app.get("/v1/receipts")
def list_receipts(ctx: AuthContext = Depends(require_scope("read"))) -> list[dict[str, Any]]:
    with db.session_scope() as s:
        q = s.query(Receipt)
        if not ctx.is_admin:
            ids = _principal_ids(s, ctx)
            q = q.filter(Receipt.principal_id.in_(ids))
        return [to_dict(r) for r in q.all()]


@app.get("/v1/receipts/{receipt_id}")
def get_receipt(receipt_id: str, ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
    with db.session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            raise _closed()
        if not ctx.is_admin:
            ids = _principal_ids(s, ctx)
            if r.principal_id not in ids:
                raise _closed()
        data = to_dict(r)
        data["valid"] = verify_receipt(r)
        return data


@app.post("/v1/keys")
def post_key(body: KeyIn, ctx: AuthContext = Depends(require_scope("admin"))) -> dict[str, Any]:
    account_id = body.account_id or ctx.account_id
    with db.session_scope() as s:
        if not ctx.is_admin and account_id != ctx.account_id:
            raise _closed()
        issued = auth.issue_api_key(
            s,
            account_id=account_id,
            kind=body.kind,
            scopes=body.scopes,
            expires_at=body.expires_at,
        )
        return auth.public_key_view(issued.record, secret=issued.secret)


def _may_manage_key(ctx: AuthContext, row: ApiKey) -> None:
    """Self may rotate/revoke own key. Sibling non-admin keys need write or admin."""
    if row is None:
        raise _closed()
    if not ctx.is_admin and row.account_id != ctx.account_id:
        raise _closed()
    scopes = json.loads(row.scopes_json or "[]")
    admin_target = row.kind == "admin" or "admin" in scopes
    if admin_target and not ctx.is_admin and ctx.api_key_id != row.id:
        raise _forbidden("admin key rotate/revoke requires admin or the same key")
    if ctx.api_key_id == row.id or ctx.is_admin:
        return
    if ctx.has_scope("write"):
        return
    raise _forbidden("key rotate/revoke requires self, write, or admin")


@app.post("/v1/keys/{key_id}/rotate")
def post_key_rotate(key_id: str, ctx: AuthContext = Depends(require_auth)) -> dict[str, Any]:
    with db.session_scope() as s:
        row = s.get(ApiKey, key_id)
        _may_manage_key(ctx, row)
        issued = auth.rotate_api_key(s, key_id)
        return auth.public_key_view(issued.record, secret=issued.secret)


@app.post("/v1/keys/{key_id}/revoke")
def post_key_revoke(key_id: str, ctx: AuthContext = Depends(require_auth)) -> dict[str, Any]:
    with db.session_scope() as s:
        row = s.get(ApiKey, key_id)
        _may_manage_key(ctx, row)
        row = auth.revoke_api_key(s, key_id)
        return auth.public_key_view(row)


@app.post("/v1/invocations/{invocation_id}/reconcile")
def post_reconcile(
    invocation_id: str,
    body: ReconcileIn,
    ctx: AuthContext = Depends(require_scope("admin")),
) -> dict[str, Any]:
    from crossing import billing, receipts
    from crossing.models import Invocation

    with db.session_scope() as s:
        inv = s.get(Invocation, invocation_id)
        if inv is None:
            raise _closed()
        if not ctx.is_admin:
            ids = _principal_ids(s, ctx)
            if inv.principal_id not in ids:
                raise _closed()
        actor = ctx.api_key_id
        if body.outcome == "committed" and billing.configured():
            try:
                billing.require_billable(s, inv.principal_id)
            except PolicyDenied as exc:
                raise HTTPException(
                    status_code=403, detail={"reason": exc.reason, "detail": exc.detail}
                ) from exc

        def receipt_fn(sess):
            return receipts.issue(
                sess,
                principal_id=inv.principal_id,
                mandate_id=inv.mandate_id,
                reservation_id=inv.reservation_id,
                tool=inv.tool,
                server=inv.server,
                amount_cents=inv.amount_cents,
                result={"reconciled": True, "evidence_ref": body.evidence_ref},
                request_hash=inv.request_hash,
                outcome="reconciled_committed",
            )

        def billing_fn(sess, rec):
            return billing.enqueue(
                sess,
                receipt_id=rec.id,
                amount_cents=inv.amount_cents,
                principal_id=inv.principal_id,
            )

        try:
            if body.outcome == "committed":
                result = ledger.reconcile_commit(
                    s,
                    inv,
                    actor=actor,
                    evidence_ref=body.evidence_ref,
                    evidence_kind=body.evidence_kind,
                    receipt_fn=receipt_fn,
                    billing_fn=billing_fn,
                )
            elif body.outcome == "released":
                result = ledger.reconcile_release(
                    s,
                    inv,
                    actor=actor,
                    evidence_ref=body.evidence_ref,
                    evidence_kind=body.evidence_kind,
                )
            else:
                raise HTTPException(status_code=400, detail="outcome must be committed or released")
        except PolicyDenied as exc:
            raise HTTPException(status_code=400, detail={"reason": exc.reason, "detail": exc.detail}) from exc
        return {
            "ok": True,
            "won": result.won,
            "status": result.invocation.status,
            "reservation_status": result.reservation.status if result.reservation is not None else None,
        }


@app.post("/v1/stripe/webhooks")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    from crossing import billing

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not billing.verify_webhook_signature(payload, sig):
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(event, dict) or not event.get("id"):
        raise HTTPException(status_code=400, detail="missing event id")
    try:
        with db.session_scope() as s:
            _row, inserted = billing.record_stripe_event(s, event)
    except IntegrityError:
        return {"ok": True, "duplicate": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "duplicate": not inserted}


@app.post("/v1/admin/accounts/{account_id}/stripe-customer")
def post_admin_stripe_customer(
    account_id: str,
    body: StripeCustomerIn,
    ctx: AuthContext = Depends(require_scope("admin")),
) -> dict[str, Any]:
    from crossing import billing

    with db.session_scope() as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise _closed()
        if not ctx.is_admin:
            raise _forbidden()
        try:
            acct = billing.attach_stripe_customer(s, account_id, body.stripe_customer_id)
        except PolicyDenied as exc:
            raise HTTPException(status_code=409, detail={"reason": exc.reason, "detail": exc.detail}) from exc
        return {
            "account_id": acct.id,
            "stripe_customer_present": bool(acct.stripe_customer_id),
        }


@app.post("/v1/admin/outbox/{outbox_id}/requeue")
def post_admin_requeue_dead(
    outbox_id: str,
    ctx: AuthContext = Depends(require_scope("admin")),
) -> dict[str, Any]:
    """Requeue a dead outbox row to pending. Worker claims it. Admin scope."""
    from crossing import billing
    from crossing.models import Outbox

    with db.session_scope() as s:
        row = s.get(Outbox, outbox_id)
        if row is None:
            raise _closed()
        if not ctx.is_admin:
            raise _forbidden()
        if row.status != "dead":
            raise HTTPException(status_code=400, detail={"reason": "not_dead"})
        billing.requeue_dead(s, outbox_id)
        s.refresh(row)
        return {"id": row.id, "status": row.status}


@app.get("/v1/billing/status")
def get_billing_status(ctx: AuthContext = Depends(require_scope("billing:read"))) -> dict[str, Any]:
    from crossing import billing

    with db.session_scope() as s:
        return billing.billing_status(s, ctx.account_id)

_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
