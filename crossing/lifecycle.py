"""quote / authorize / reserve / execute / commit / release."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing import billing, ledger, mock_mcp, pricing, receipts
from crossing.identity import create_task
from crossing.mandate import load_live_mandate
from crossing.models import IdempotencyRecord, UsedNonce, new_id
from crossing.policy import PolicyDenied, Reason, check_tool


@dataclass
class InvokeResult:
    ok: bool
    reason: str | None = None
    detail: str = ""
    amount_cents: int = 0
    remaining_cents: int | None = None
    receipt: dict[str, Any] | None = None
    result: Any = None
    reservation_id: str | None = None
    idempotency_key: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "amount_cents": self.amount_cents,
            "remaining_cents": self.remaining_cents,
            "receipt": self.receipt,
            "result": self.result,
            "reservation_id": self.reservation_id,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
        }


def quote(tool: str, server: str = pricing.DEFAULT_SERVER) -> int:
    try:
        return pricing.quote(tool, server)
    except KeyError as exc:
        raise PolicyDenied(Reason.UNKNOWN_TOOL, str(exc)) from exc


def authorize(session: Session, mandate_id: str, tool: str, server: str = pricing.DEFAULT_SERVER) -> tuple[Any, int]:
    mandate = load_live_mandate(session, mandate_id)
    price = quote(tool, server)
    check_tool(mandate, tool, server, price)
    return mandate, price


def _load_idem(session: Session, principal_id: str, key: str | None) -> InvokeResult | None:
    if not key:
        return None
    row = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.principal_id == principal_id,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if row is None:
        return None
    data = json.loads(row.result_json)
    res = InvokeResult(**{k: data[k] for k in data if k in InvokeResult.__dataclass_fields__})
    res.replayed = True
    return res


def invoke(
    session: Session,
    *,
    mandate_id: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    server: str = pricing.DEFAULT_SERVER,
    idempotency_key: str | None = None,
    nonce: str | None = None,
    task_name: str | None = None,
) -> InvokeResult:
    try:
        mandate = load_live_mandate(session, mandate_id)
    except PolicyDenied as exc:
        return InvokeResult(ok=False, reason=exc.reason, detail=str(exc.detail))

    cached = _load_idem(session, mandate.principal_id, idempotency_key)
    if cached is not None:
        return cached

    if nonce:
        clash = session.scalar(
            select(UsedNonce).where(UsedNonce.principal_id == mandate.principal_id, UsedNonce.nonce == nonce)
        )
        if clash is not None:
            ledger.append_event(
                session,
                principal_id=mandate.principal_id,
                mandate_id=mandate.id,
                kind="deny",
                note=Reason.NONCE_REPLAY,
            )
            return InvokeResult(ok=False, reason=Reason.NONCE_REPLAY, detail=nonce)

    try:
        price = quote(tool, server)
        check_tool(mandate, tool, server, price)
    except PolicyDenied as exc:
        ledger.append_event(
            session,
            principal_id=mandate.principal_id,
            mandate_id=mandate.id,
            kind="deny",
            amount_cents=0,
            remaining_after=mandate.remaining_cents,
            note=exc.reason,
            idempotency_key=idempotency_key,
        )
        return InvokeResult(
            ok=False,
            reason=exc.reason,
            detail=str(exc.detail),
            remaining_cents=mandate.remaining_cents,
            idempotency_key=idempotency_key,
        )

    if nonce:
        session.add(UsedNonce(id=new_id(), principal_id=mandate.principal_id, nonce=nonce))

    if task_name:
        create_task(session, mandate.principal_id, mandate.agent_id, task_name)

    reservation = None
    try:
        reservation = ledger.reserve(
            session, mandate, price, idempotency_key=idempotency_key, nonce=nonce
        )
    except PolicyDenied as exc:
        return InvokeResult(
            ok=False,
            reason=exc.reason,
            detail=str(exc.detail),
            amount_cents=price,
            remaining_cents=mandate.remaining_cents,
            idempotency_key=idempotency_key,
        )

    try:
        tool_result = mock_mcp.call_tool(tool, arguments or {})
    except Exception as exc:
        ledger.release(session, reservation)
        return InvokeResult(
            ok=False,
            reason="MCP_ERROR",
            detail=exc.__class__.__name__,
            amount_cents=price,
            remaining_cents=session.get(type(mandate), mandate.id).remaining_cents if mandate else None,
            reservation_id=reservation.id,
            idempotency_key=idempotency_key,
        )

    rec = receipts.issue(
        session,
        principal_id=mandate.principal_id,
        mandate_id=mandate.id,
        reservation_id=reservation.id,
        tool=tool,
        server=server,
        amount_cents=price,
        result=tool_result,
    )
    ledger.commit(session, reservation)
    row = billing.enqueue(
        session,
        receipt_id=rec.id,
        amount_cents=price,
        principal_id=mandate.principal_id,
    )
    billing.report_after_commit(session, row)

    result = InvokeResult(
        ok=True,
        amount_cents=price,
        remaining_cents=session.get(type(mandate), mandate.id).remaining_cents,
        receipt=receipts.to_dict(rec),
        result=tool_result,
        reservation_id=reservation.id,
        idempotency_key=idempotency_key,
    )
    if idempotency_key:
        session.add(
            IdempotencyRecord(
                id=new_id(),
                principal_id=mandate.principal_id,
                idempotency_key=idempotency_key,
                result_json=json.dumps(result.to_dict()),
            )
        )
        session.flush()
    return result
