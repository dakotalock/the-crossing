from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from crossing import auth, crypto, db, ledger
from crossing.auth import AuthContext, SESSION_COOKIE
from crossing.dashboard import render
from crossing.identity import create_agent, create_principal, revoke_agent
from crossing.lifecycle import invoke
from crossing.mandate import issue_mandate, revoke_mandate
from crossing.models import Agent, ApiKey, Mandate, Principal, Receipt
from crossing.policy import PolicyDenied, Reason
from crossing.receipts import to_dict, verify_receipt


def _cors_origins() -> list[str]:
    raw = os.environ.get("CROSSING_CORS_ORIGINS") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _boot() -> None:
    if os.environ.get("CROSSING_ALLOW_DEV") != "1":
        crypto.require_production_secrets()
        auth.key_pepper()
    if db.SessionLocal is None:
        db.init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _boot()
    yield


app = FastAPI(title="The Crossing", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
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


@app.post("/v1/principals")
def post_principal(body: PrincipalIn, ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
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
def post_agent(body: AgentIn, ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
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
def post_revoke(agent_id: str, ctx: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
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


@app.post("/v1/keys/{key_id}/rotate")
def post_key_rotate(key_id: str, ctx: AuthContext = Depends(require_auth)) -> dict[str, Any]:
    with db.session_scope() as s:
        row = s.get(ApiKey, key_id)
        if row is None:
            raise _closed()
        if not ctx.is_admin and row.account_id != ctx.account_id:
            raise _closed()
        issued = auth.rotate_api_key(s, key_id)
        return auth.public_key_view(issued.record, secret=issued.secret)


@app.post("/v1/keys/{key_id}/revoke")
def post_key_revoke(key_id: str, ctx: AuthContext = Depends(require_auth)) -> dict[str, Any]:
    with db.session_scope() as s:
        row = s.get(ApiKey, key_id)
        if row is None:
            raise _closed()
        if not ctx.is_admin and row.account_id != ctx.account_id:
            raise _closed()
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
        billing.drain_outbox()
        return {
            "ok": True,
            "won": result.won,
            "status": result.invocation.status,
            "reservation_status": result.reservation.status if result.reservation is not None else None,
        }
