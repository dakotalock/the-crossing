"""Append-only ledger and atomic budget reservations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from crossing.models import Invocation, LedgerEvent, Mandate, Reservation, new_id
from crossing.policy import PolicyDenied, Reason


def append_event(
    session: Session,
    *,
    principal_id: str,
    mandate_id: str,
    kind: str,
    amount_cents: int = 0,
    remaining_after: int | None = None,
    reservation_id: str | None = None,
    idempotency_key: str | None = None,
    note: str | None = None,
    task_id: str | None = None,
) -> LedgerEvent:
    ev = LedgerEvent(
        id=new_id(),
        principal_id=principal_id,
        mandate_id=mandate_id,
        reservation_id=reservation_id,
        task_id=task_id,
        kind=kind,
        amount_cents=amount_cents,
        remaining_after=remaining_after,
        idempotency_key=idempotency_key,
        note=note,
    )
    session.add(ev)
    session.flush()
    return ev


def reserve(
    session: Session,
    mandate: Mandate,
    amount_cents: int,
    *,
    idempotency_key: str | None = None,
    nonce: str | None = None,
    task_id: str | None = None,
) -> Reservation:
    if amount_cents < 0:
        raise PolicyDenied(Reason.INVALID_AMOUNT, "reserve amount must be >= 0")
    locked = session.execute(select(Mandate).where(Mandate.id == mandate.id)).scalar_one()
    if locked.remaining_cents < amount_cents:
        append_event(
            session,
            principal_id=locked.principal_id,
            mandate_id=locked.id,
            kind="deny",
            amount_cents=amount_cents,
            remaining_after=locked.remaining_cents,
            idempotency_key=idempotency_key,
            note=Reason.BUDGET_EXCEEDED,
            task_id=task_id,
        )
        raise PolicyDenied(Reason.BUDGET_EXCEEDED, f"need {amount_cents} have {locked.remaining_cents}")
    locked.remaining_cents -= amount_cents
    locked.calls_used += 1
    res = Reservation(
        id=new_id(),
        principal_id=locked.principal_id,
        mandate_id=locked.id,
        amount_cents=amount_cents,
        status="held",
        idempotency_key=idempotency_key,
        nonce=nonce,
    )
    session.add(res)
    session.flush()
    append_event(
        session,
        principal_id=locked.principal_id,
        mandate_id=locked.id,
        kind="reserve",
        amount_cents=amount_cents,
        remaining_after=locked.remaining_cents,
        reservation_id=res.id,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )
    mandate.remaining_cents = locked.remaining_cents
    mandate.calls_used = locked.calls_used
    return res


def reserve_and_commit(
    session: Session,
    mandate: Mandate,
    amount_cents: int,
    *,
    idempotency_key: str | None = None,
    nonce: str | None = None,
    tool: str = "",
    server: str = "",
    request_hash: str | None = None,
    task_id: str | None = None,
) -> tuple[Reservation, Invocation]:
    """Write reservation + decrement remaining, then COMMIT before external execute.

    Crash-after-execute-before-commit leaves this reserved Invocation row durable.
    It must not silently roll back the reserve.
    """
    res = reserve(
        session,
        mandate,
        amount_cents,
        idempotency_key=idempotency_key,
        nonce=nonce,
        task_id=task_id,
    )
    inv = Invocation(
        id=new_id(),
        principal_id=mandate.principal_id,
        mandate_id=mandate.id,
        reservation_id=res.id,
        task_id=task_id,
        tool=tool,
        server=server,
        request_hash=request_hash,
        idempotency_key=idempotency_key,
        amount_cents=amount_cents,
        status="reserved",
    )
    session.add(inv)
    session.flush()
    session.commit()
    return res, inv


def recover_reserved(session: Session, invocation: Invocation, *, mode: str = "release") -> Invocation:
    """Recover reserved+no-outcome rows.

    Conservative default (`release`): refund remaining and mark released.
    `ambiguous`: mark ambiguous when the tool side-effect may have occurred
    (crash after execute, before commit). Operators inspect and resolve.
    """
    if invocation.status != "reserved":
        return invocation
    if mode == "ambiguous":
        invocation.status = "ambiguous"
        session.flush()
        return invocation
    res = session.get(Reservation, invocation.reservation_id) if invocation.reservation_id else None
    if res is not None:
        release(session, res, task_id=invocation.task_id)
    invocation.status = "released"
    session.flush()
    return invocation


def commit(session: Session, reservation: Reservation, *, task_id: str | None = None) -> Reservation:
    if reservation.status != "held":
        return reservation
    reservation.status = "committed"
    m = session.get(Mandate, reservation.mandate_id)
    remaining = m.remaining_cents if m else None
    append_event(
        session,
        principal_id=reservation.principal_id,
        mandate_id=reservation.mandate_id,
        kind="commit",
        amount_cents=reservation.amount_cents,
        remaining_after=remaining,
        reservation_id=reservation.id,
        idempotency_key=reservation.idempotency_key,
        task_id=task_id,
    )
    session.flush()
    return reservation


def release(session: Session, reservation: Reservation, *, task_id: str | None = None) -> Reservation:
    if reservation.status != "held":
        return reservation
    reservation.status = "released"
    m = session.get(Mandate, reservation.mandate_id)
    if m is not None:
        m.remaining_cents += reservation.amount_cents
        if m.calls_used > 0:
            m.calls_used -= 1
        remaining = m.remaining_cents
    else:
        remaining = None
    append_event(
        session,
        principal_id=reservation.principal_id,
        mandate_id=reservation.mandate_id,
        kind="release",
        amount_cents=reservation.amount_cents,
        remaining_after=remaining,
        reservation_id=reservation.id,
        idempotency_key=reservation.idempotency_key,
        task_id=task_id,
    )
    session.flush()
    return reservation
