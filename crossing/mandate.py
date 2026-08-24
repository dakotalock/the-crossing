from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing import crypto
from crossing.identity import require_live_agent
from crossing.models import LedgerEvent, Mandate, UsedNonce, new_id, utcnow
from crossing.policy import (
    PolicyDenied,
    Reason,
    as_utc,
    check_child_attenuation,
    check_fresh,
    check_mandate_signature,
)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def mandate_payload(
    *,
    principal_id: str,
    agent_id: str,
    parent_mandate_id: str | None,
    spend_limit_cents: int,
    max_call_cents: int,
    max_calls: int | None,
    tools: list[str] | None,
    servers: list[str] | None,
    expires_at: datetime | None,
    max_subagent_budget_cents: int | None,
    nonce: str,
) -> dict[str, Any]:
    exp = as_utc(expires_at)
    return {
        "v": 1,
        "principal_id": principal_id,
        "agent_id": agent_id,
        "parent_mandate_id": parent_mandate_id,
        "spend_limit_cents": spend_limit_cents,
        "max_call_cents": max_call_cents,
        "max_calls": max_calls,
        "tools": sorted(tools) if tools is not None else None,
        "servers": sorted(servers) if servers is not None else None,
        "expires_at": exp.isoformat() if exp else None,
        "max_subagent_budget_cents": max_subagent_budget_cents,
        "nonce": nonce,
    }


def issue_mandate(
    session: Session,
    *,
    principal_id: str,
    agent_id: str,
    spend_limit_cents: int,
    max_call_cents: int | None = None,
    max_calls: int | None = 1000,
    tools: list[str] | None = None,
    servers: list[str] | None = None,
    expires_at: datetime | None = None,
    max_subagent_budget_cents: int | None = None,
    nonce: str | None = None,
    parent_mandate_id: str | None = None,
    signature: str | None = None,
    pubkey_hex: str | None = None,
    verify: bool = True,
) -> Mandate:
    require_live_agent(session, agent_id)
    nonce = nonce or secrets.token_hex(16)
    existing = session.scalar(select(UsedNonce).where(UsedNonce.principal_id == principal_id, UsedNonce.nonce == nonce))
    if existing is not None:
        raise PolicyDenied(Reason.NONCE_REPLAY, nonce)
    session.add(UsedNonce(id=new_id(), principal_id=principal_id, nonce=nonce))

    if max_call_cents is None:
        max_call_cents = spend_limit_cents
    if max_subagent_budget_cents is None:
        max_subagent_budget_cents = spend_limit_cents

    parent = None
    if parent_mandate_id:
        parent = session.get(Mandate, parent_mandate_id)
        if parent is None:
            raise PolicyDenied(Reason.MANDATE_REVOKED, "parent mandate missing")
        if verify:
            check_mandate_signature(parent, crypto.verify_obj)
        check_fresh(parent)
        check_child_attenuation(
            parent,
            spend_limit_cents=spend_limit_cents,
            max_call_cents=max_call_cents,
            tools=tools,
            servers=servers,
            expires_at=expires_at,
            max_subagent_budget_cents=max_subagent_budget_cents,
        )

    payload = mandate_payload(
        principal_id=principal_id,
        agent_id=agent_id,
        parent_mandate_id=parent_mandate_id,
        spend_limit_cents=spend_limit_cents,
        max_call_cents=max_call_cents,
        max_calls=max_calls,
        tools=tools,
        servers=servers,
        expires_at=expires_at,
        max_subagent_budget_cents=max_subagent_budget_cents,
        nonce=nonce,
    )
    if signature is None:
        signature = crypto.sign_obj(payload)
        pubkey_hex = crypto.pubkey_hex()
    elif pubkey_hex is None:
        pubkey_hex = crypto.pubkey_hex()

    m = Mandate(
        id=new_id(),
        principal_id=principal_id,
        agent_id=agent_id,
        parent_mandate_id=parent_mandate_id,
        spend_limit_cents=spend_limit_cents,
        remaining_cents=spend_limit_cents,
        max_call_cents=max_call_cents,
        max_calls=max_calls,
        calls_used=0,
        tools_json=_dumps(tools) if tools is not None else None,
        servers_json=_dumps(servers) if servers is not None else None,
        expires_at=as_utc(expires_at),
        max_subagent_budget_cents=max_subagent_budget_cents,
        nonce=nonce,
        signature=signature,
        pubkey_hex=pubkey_hex,
        payload_json=_dumps(payload),
        revoked=False,
    )
    if verify:
        check_mandate_signature(m, crypto.verify_obj)

    session.add(m)
    session.flush()

    if parent is not None:
        parent.remaining_cents -= spend_limit_cents
        session.add(
            LedgerEvent(
                id=new_id(),
                principal_id=principal_id,
                mandate_id=parent.id,
                kind="escrow",
                amount_cents=spend_limit_cents,
                remaining_after=parent.remaining_cents,
                note=f"child={m.id}",
            )
        )
        session.flush()
    return m


def load_live_mandate(session: Session, mandate_id: str, *, verify: bool = True) -> Mandate:
    m = session.get(Mandate, mandate_id)
    if m is None:
        raise PolicyDenied(Reason.MANDATE_REVOKED, "mandate missing")
    if verify:
        check_mandate_signature(m, crypto.verify_obj)
    check_fresh(m)
    require_live_agent(session, m.agent_id)
    return m
