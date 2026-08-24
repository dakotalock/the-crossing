"""quote / authorize / reserve_and_commit / mark_executing / execute / finalize_success | finalize_release."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing import abuse, billing, ledger, metrics, pricing, providers, receipts
from crossing.identity import create_task
from crossing.ledger import IdempotencyReplay
from crossing.mandate import load_live_mandate
from crossing.models import IdempotencyRecord, UsedNonce, new_id
from crossing.policy import PolicyDenied, Reason, check_tool
from crossing.providers import ProviderError
from crossing.receipts import payload_hash


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
    task_id: str | None = None

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
            "task_id": self.task_id,
        }


def request_fingerprint(mandate_id: str, server: str, tool: str, arguments: dict[str, Any] | None) -> str:
    return payload_hash(
        {
            "mandate_id": mandate_id,
            "server": server,
            "tool": tool,
            "arguments": arguments or {},
        }
    )


def quote(tool: str, server: str = pricing.DEFAULT_SERVER) -> int:
    try:
        return providers.quote(tool, server)
    except KeyError as exc:
        raise PolicyDenied(Reason.UNKNOWN_TOOL, str(exc)) from exc


def authorize(session: Session, mandate_id: str, tool: str, server: str = pricing.DEFAULT_SERVER) -> tuple[Any, int]:
    mandate = load_live_mandate(session, mandate_id)
    price = quote(tool, server)
    check_tool(mandate, tool, server, price)
    return mandate, price


def _load_idem(
    session: Session, principal_id: str, key: str | None, request_hash: str
) -> InvokeResult | None:
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
    stored = row.request_hash
    if stored and stored != request_hash:
        raise PolicyDenied(Reason.IDEMPOTENCY_CONFLICT, "same key, different request hash")
    if row.status == "in_progress" or not row.result_json:
        raise PolicyDenied(Reason.IN_PROGRESS, "wait-or-conflict")
    data = json.loads(row.result_json)
    res = InvokeResult(**{k: data[k] for k in data if k in InvokeResult.__dataclass_fields__})
    res.replayed = True
    return res


def _deny(
    session: Session,
    *,
    principal_id: str | None,
    mandate_id: str,
    reason: str,
    detail: str = "",
    remaining: int | None = None,
    amount: int = 0,
    idempotency_key: str | None = None,
    task_id: str | None = None,
) -> InvokeResult:
    metrics.inc_deny(reason)
    if principal_id:
        ledger.append_event(
            session,
            principal_id=principal_id,
            mandate_id=mandate_id,
            kind="deny",
            amount_cents=amount,
            remaining_after=remaining,
            note=reason,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )
    return InvokeResult(
        ok=False,
        reason=reason,
        detail=detail,
        amount_cents=amount,
        remaining_cents=remaining,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )


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
    task_id: str | None = None,
) -> InvokeResult:
    req_hash = request_fingerprint(mandate_id, server, tool, arguments)
    from crossing.models import Mandate

    try:
        mandate = load_live_mandate(session, mandate_id)
    except PolicyDenied as exc:
        m = session.get(Mandate, mandate_id)
        return _deny(
            session,
            principal_id=m.principal_id if m else None,
            mandate_id=mandate_id,
            reason=exc.reason,
            detail=str(exc.detail),
            remaining=m.remaining_cents if m else None,
            idempotency_key=idempotency_key,
        )

    try:
        cached = _load_idem(session, mandate.principal_id, idempotency_key, req_hash)
    except PolicyDenied as exc:
        if exc.reason == Reason.IN_PROGRESS:
            return InvokeResult(
                ok=False,
                reason=exc.reason,
                detail=str(exc.detail),
                remaining_cents=mandate.remaining_cents,
                idempotency_key=idempotency_key,
            )
        return _deny(
            session,
            principal_id=mandate.principal_id,
            mandate_id=mandate.id,
            reason=exc.reason,
            detail=str(exc.detail),
            remaining=mandate.remaining_cents,
            idempotency_key=idempotency_key,
        )
    if cached is not None:
        return cached

    if nonce:
        clash = session.scalar(
            select(UsedNonce).where(UsedNonce.principal_id == mandate.principal_id, UsedNonce.nonce == nonce)
        )
        if clash is not None:
            return _deny(
                session,
                principal_id=mandate.principal_id,
                mandate_id=mandate.id,
                reason=Reason.NONCE_REPLAY,
                detail=nonce,
                remaining=mandate.remaining_cents,
                idempotency_key=idempotency_key,
            )

    try:
        price = quote(tool, server)
        check_tool(mandate, tool, server, price)
        abuse.validate_arguments(tool, arguments)
    except PolicyDenied as exc:
        return _deny(
            session,
            principal_id=mandate.principal_id,
            mandate_id=mandate.id,
            reason=exc.reason,
            detail=str(exc.detail),
            remaining=mandate.remaining_cents,
            idempotency_key=idempotency_key,
        )

    if nonce:
        session.add(UsedNonce(id=new_id(), principal_id=mandate.principal_id, nonce=nonce))

    if task_id is None:
        task = create_task(session, mandate.principal_id, mandate.agent_id, task_name or "invoke")
        task_id = task.id

    try:
        reservation, invocation = ledger.reserve_and_commit(
            session,
            mandate,
            price,
            idempotency_key=idempotency_key,
            nonce=nonce,
            tool=tool,
            server=server,
            request_hash=req_hash,
            task_id=task_id,
        )
    except IdempotencyReplay as replay:
        data = json.loads(replay.record.result_json or "{}")
        res = InvokeResult(**{k: data[k] for k in data if k in InvokeResult.__dataclass_fields__})
        res.replayed = True
        return res
    except PolicyDenied as exc:
        return InvokeResult(
            ok=False,
            reason=exc.reason,
            detail=str(exc.detail),
            amount_cents=price,
            remaining_cents=mandate.remaining_cents,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    # Durable dispatch: reserved → executing COMMITTED before any provider I/O.
    marked = ledger.mark_executing(session, invocation)
    if not marked.won:
        return InvokeResult(
            ok=False,
            reason=marked.reason or Reason.IN_PROGRESS,
            detail=f"dispatch not started; status={invocation.status}",
            amount_cents=price,
            remaining_cents=session.get(type(mandate), mandate.id).remaining_cents if mandate else None,
            reservation_id=reservation.id,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    try:
        tool_result = providers.invoke(
            server,
            tool,
            arguments or {},
            invocation_id=invocation.id,
            idempotency_key=idempotency_key,
        )
    except (ProviderError, Exception) as exc:
        # Provider may have run. Do not refund or clear the claim.
        ledger.mark_executed_fail(session, invocation)
        return InvokeResult(
            ok=False,
            reason="MCP_ERROR",
            detail=exc.__class__.__name__,
            amount_cents=price,
            remaining_cents=session.get(type(mandate), mandate.id).remaining_cents if mandate else None,
            reservation_id=reservation.id,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    def _issue_receipt(s):
        return receipts.issue(
            s,
            principal_id=mandate.principal_id,
            mandate_id=mandate.id,
            reservation_id=reservation.id,
            tool=tool,
            server=server,
            amount_cents=price,
            result=tool_result,
            agent_id=mandate.agent_id,
            task_id=task_id,
            request_hash=req_hash,
            outcome="ok",
        )

    def _enqueue_billing(s, rec):
        return billing.enqueue(
            s,
            receipt_id=rec.id,
            amount_cents=price,
            principal_id=mandate.principal_id,
        )

    remaining_now = session.get(type(mandate), mandate.id).remaining_cents

    def _result_payload(rec):
        built = InvokeResult(
            ok=True,
            amount_cents=price,
            remaining_cents=remaining_now,
            receipt=receipts.to_dict(rec) if rec is not None else None,
            result=tool_result,
            reservation_id=reservation.id,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )
        return json.dumps(built.to_dict())

    fin = ledger.finalize_success(
        session,
        reservation,
        invocation,
        receipt_fn=_issue_receipt,
        billing_fn=_enqueue_billing,
        result_fn=_result_payload,
    )
    if not fin.won:
        return InvokeResult(
            ok=False,
            reason="FINALIZE_LOST",
            detail="terminal transition lost",
            amount_cents=price,
            remaining_cents=session.get(type(mandate), mandate.id).remaining_cents if mandate else None,
            reservation_id=reservation.id,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    rec = fin.receipt
    result = InvokeResult(
        ok=True,
        amount_cents=price,
        remaining_cents=session.get(type(mandate), mandate.id).remaining_cents,
        receipt=receipts.to_dict(rec) if rec is not None else None,
        result=tool_result,
        reservation_id=reservation.id,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )
    billing.drain_outbox()
    metrics.inc_invoke()
    return result
