from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from crossing import crypto, db
from crossing.dashboard import render
from crossing.identity import create_agent, create_principal, revoke_agent
from crossing.lifecycle import invoke
from crossing.mandate import issue_mandate
from crossing.models import Agent, Mandate, Principal, Receipt
from crossing.policy import PolicyDenied
from crossing.receipts import to_dict, verify_receipt


def _boot() -> None:
    if os.environ.get("CROSSING_ALLOW_DEV") != "1":
        crypto.require_production_secrets()
    if db.SessionLocal is None:
        db.init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _boot()
    yield


app = FastAPI(title="The Crossing", version="0.1.0", lifespan=lifespan)


class PrincipalIn(BaseModel):
    name: str


class AgentIn(BaseModel):
    principal_id: str
    name: str
    parent_id: str | None = None


class MandateIn(BaseModel):
    principal_id: str
    agent_id: str
    spend_limit_cents: int
    max_call_cents: int | None = None
    max_calls: int | None = 1000
    tools: list[str] | None = None
    servers: list[str] | None = None
    expires_at: datetime | None = None
    max_subagent_budget_cents: int | None = None
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    _boot()
    with db.session_scope() as s:
        return render(s)


@app.post("/v1/principals")
def post_principal(body: PrincipalIn) -> dict[str, Any]:
    _boot()
    with db.session_scope() as s:
        p = create_principal(s, body.name)
        return {"id": p.id, "name": p.name}


@app.get("/v1/principals")
def list_principals() -> list[dict[str, Any]]:
    _boot()
    with db.session_scope() as s:
        rows = s.query(Principal).all()
        return [{"id": p.id, "name": p.name} for p in rows]


@app.post("/v1/agents")
def post_agent(body: AgentIn) -> dict[str, Any]:
    _boot()
    try:
        with db.session_scope() as s:
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
def post_revoke(agent_id: str) -> dict[str, Any]:
    _boot()
    with db.session_scope() as s:
        a = revoke_agent(s, agent_id)
        return {"id": a.id, "revoked": a.revoked}


@app.get("/v1/agents")
def list_agents() -> list[dict[str, Any]]:
    _boot()
    with db.session_scope() as s:
        return [
            {"id": a.id, "principal_id": a.principal_id, "parent_id": a.parent_id, "name": a.name, "revoked": a.revoked}
            for a in s.query(Agent).all()
        ]


@app.post("/v1/mandates")
def post_mandate(body: MandateIn) -> dict[str, Any]:
    _boot()
    try:
        with db.session_scope() as s:
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
def get_mandate(mandate_id: str) -> dict[str, Any]:
    _boot()
    with db.session_scope() as s:
        m = s.get(Mandate, mandate_id)
        if not m:
            raise HTTPException(404, "not found")
        return {
            "id": m.id,
            "remaining_cents": m.remaining_cents,
            "spend_limit_cents": m.spend_limit_cents,
            "tools": m.tools_list(),
            "revoked": m.revoked,
        }


@app.post("/v1/invoke")
def post_invoke(body: InvokeIn) -> dict[str, Any]:
    _boot()
    with db.session_scope() as s:
        result = invoke(s, **body.model_dump())
        if not result.ok:
            raise HTTPException(status_code=403, detail=result.to_dict())
        return result.to_dict()


@app.get("/v1/receipts")
def list_receipts() -> list[dict[str, Any]]:
    _boot()
    with db.session_scope() as s:
        return [to_dict(r) for r in s.query(Receipt).all()]


@app.get("/v1/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict[str, Any]:
    _boot()
    with db.session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            raise HTTPException(404, "not found")
        data = to_dict(r)
        data["valid"] = verify_receipt(r)
        return data
