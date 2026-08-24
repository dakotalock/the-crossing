from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from crossing import crypto
from crossing.identity import is_self_or_descendant, require_live_agent
from crossing.models import LedgerEvent, Mandate, Principal, UsedNonce, new_id
from crossing.policy import (
    PolicyDenied,
    Reason,
    as_utc,
    bound_pubkeys,
    check_child_attenuation,
    check_fresh,
    check_mandate_signature,
    check_non_negative_money,
    check_signed_state,
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
    """Immutable signed fields only.

    remaining_cents and calls_used are mutable accounting and MUST NOT be signed.
    remaining starts equal to signed spend_limit_cents.
    """
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


def _verify_caller_signature(
    session: Session,
    *,
    principal_id: str,
    payload: dict[str, Any],
    signature: str,
    pubkey_hex: str | None,
) -> str:
    """Return the bound pubkey used to verify. Never trust a request pubkey unless bound."""
    principal = session.get(Principal, principal_id)
    issuer = crypto.pubkey_hex()
    allowed = bound_pubkeys(principal, issuer)
    if pubkey_hex is not None and pubkey_hex not in allowed:
        raise PolicyDenied(Reason.MANDATE_FORGED, "request pubkey is not bound to the principal")
    registered = principal.pubkey_hex if principal is not None else None
    verify_key = registered if registered else issuer
    if not crypto.verify_obj(payload, signature, verify_key):
        raise PolicyDenied(Reason.MANDATE_FORGED, "caller signature rejected")
    return verify_key


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
    check_non_negative_money(
        spend_limit_cents=spend_limit_cents,
        max_call_cents=max_call_cents,
        max_subagent_budget_cents=max_subagent_budget_cents,
        remaining_cents=spend_limit_cents,
    )
    agent = require_live_agent(session, agent_id)
    if agent.principal_id != principal_id:
        raise PolicyDenied(Reason.AGENT_PRINCIPAL_MISMATCH, "agent is not bound to principal")

    if max_call_cents is None:
        max_call_cents = spend_limit_cents
    if max_subagent_budget_cents is None:
        max_subagent_budget_cents = spend_limit_cents
    check_non_negative_money(max_call_cents=max_call_cents, max_subagent_budget_cents=max_subagent_budget_cents)

    parent = None
    if parent_mandate_id:
        parent = session.get(Mandate, parent_mandate_id)
        if parent is None:
            raise PolicyDenied(Reason.MANDATE_REVOKED, "parent mandate missing")
        if verify:
            _verify_stored_mandate(session, parent)
        check_fresh(parent)
        if not is_self_or_descendant(session, agent_id, parent.agent_id):
            raise PolicyDenied(
                Reason.CHILD_AGENT_NOT_DESCENDANT,
                "child agent is not the parent agent or a descendant",
            )
        check_child_attenuation(
            parent,
            spend_limit_cents=spend_limit_cents,
            max_call_cents=max_call_cents,
            tools=tools,
            servers=servers,
            expires_at=expires_at,
            max_subagent_budget_cents=max_subagent_budget_cents,
        )

    nonce = nonce or secrets.token_hex(16)
    existing = session.scalar(
        select(UsedNonce).where(UsedNonce.principal_id == principal_id, UsedNonce.nonce == nonce)
    )
    if existing is not None:
        raise PolicyDenied(Reason.NONCE_REPLAY, nonce)
    session.add(UsedNonce(id=new_id(), principal_id=principal_id, nonce=nonce))

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

    # Root mandates are signed ONLY by The Crossing issuer key unless a principal
    # key is registered and the caller proved possession of it. Request pubkeys
    # are never a trust root.
    if signature is None:
        stored_sig = crypto.sign_obj(payload)
        stored_pub = crypto.pubkey_hex()
    elif verify:
        stored_pub = _verify_caller_signature(
            session,
            principal_id=principal_id,
            payload=payload,
            signature=signature,
            pubkey_hex=pubkey_hex,
        )
        stored_sig = signature
    else:
        stored_sig = signature
        stored_pub = pubkey_hex or crypto.pubkey_hex()

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
        signature=stored_sig,
        pubkey_hex=stored_pub,
        payload_json=_dumps(payload),
        revoked=False,
    )
    if verify:
        _verify_stored_mandate(session, m)

    session.add(m)
    session.flush()

    if parent is not None:
        escrowed = session.execute(
            update(Mandate)
            .where(
                Mandate.id == parent.id,
                Mandate.remaining_cents >= spend_limit_cents,
                Mandate.revoked.is_(False),
            )
            .values(remaining_cents=Mandate.remaining_cents - spend_limit_cents)
            .returning(Mandate.remaining_cents)
        ).first()
        if escrowed is None:
            raise PolicyDenied(
                Reason.CHILD_SPEND_ESCALATION,
                f"{spend_limit_cents} > remaining",
            )
        session.refresh(parent)
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


def _verify_stored_mandate(session: Session, mandate: Mandate) -> None:
    principal = session.get(Principal, mandate.principal_id)
    issuer = crypto.pubkey_hex()
    allowed = bound_pubkeys(principal, issuer)
    check_mandate_signature(mandate, crypto.verify_obj, allowed_pubkeys=allowed)
    check_signed_state(mandate)


def load_live_mandate(session: Session, mandate_id: str, *, verify: bool = True) -> Mandate:
    m = session.get(Mandate, mandate_id)
    if m is None:
        raise PolicyDenied(Reason.MANDATE_REVOKED, "mandate missing")
    if verify:
        _verify_stored_mandate(session, m)
    check_fresh(m)
    require_live_agent(session, m.agent_id)
    return m


def revoke_mandate(session: Session, mandate_id: str) -> Mandate:
    m = session.get(Mandate, mandate_id)
    if m is None:
        raise PolicyDenied(Reason.MANDATE_REVOKED, "mandate missing")
    m.revoked = True
    session.flush()
    return m
