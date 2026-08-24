"""Append-only ledger and atomic budget reservations."""

from __future__ import annotations

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crossing.models import IdempotencyRecord, Invocation, LedgerEvent, Mandate, Reservation, new_id
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
    record_deny: bool = True,
) -> Reservation:
    """Atomically debit remaining_cents when the row still has enough budget.

    Single conditional UPDATE (SQLite and Postgres):
      UPDATE mandates SET remaining_cents = remaining_cents - :cost,
                          calls_used = calls_used + 1
      WHERE id = :id AND remaining_cents >= :cost AND revoked = 0
        AND (max_calls IS NULL OR calls_used < max_calls)
      RETURNING remaining_cents, calls_used
    """
    if amount_cents < 0:
        raise PolicyDenied(Reason.INVALID_AMOUNT, "reserve amount must be >= 0")
    row = session.execute(
        update(Mandate)
        .where(
            Mandate.id == mandate.id,
            Mandate.remaining_cents >= amount_cents,
            Mandate.revoked.is_(False),
            or_(Mandate.max_calls.is_(None), Mandate.calls_used < Mandate.max_calls),
        )
        .values(
            remaining_cents=Mandate.remaining_cents - amount_cents,
            calls_used=Mandate.calls_used + 1,
        )
        .returning(Mandate.remaining_cents, Mandate.calls_used)
    ).first()
    if row is None:
        locked = session.get(Mandate, mandate.id)
        remaining = locked.remaining_cents if locked is not None else 0
        principal_id = locked.principal_id if locked is not None else mandate.principal_id
        mid = locked.id if locked is not None else mandate.id
        reason = _reserve_deny_reason(locked, amount_cents)
        if record_deny:
            append_event(
                session,
                principal_id=principal_id,
                mandate_id=mid,
                kind="deny",
                amount_cents=amount_cents,
                remaining_after=remaining,
                idempotency_key=idempotency_key,
                note=reason,
                task_id=task_id,
            )
        raise PolicyDenied(reason, f"need {amount_cents} have {remaining}")
    session.refresh(mandate)
    res = Reservation(
        id=new_id(),
        principal_id=mandate.principal_id,
        mandate_id=mandate.id,
        amount_cents=amount_cents,
        status="held",
        idempotency_key=idempotency_key,
        nonce=nonce,
    )
    session.add(res)
    session.flush()
    append_event(
        session,
        principal_id=mandate.principal_id,
        mandate_id=mandate.id,
        kind="reserve",
        amount_cents=amount_cents,
        remaining_after=mandate.remaining_cents,
        reservation_id=res.id,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )
    return res


def _reserve_deny_reason(locked: Mandate | None, amount_cents: int) -> str:
    """Pick PolicyDenied reason after a failed atomic debit.

    Missing / revoked keeps the historical BUDGET_EXCEEDED deny (existing
    reserve() behavior). Otherwise distinguish budget vs max_calls.
    """
    if locked is None or locked.revoked:
        return Reason.BUDGET_EXCEEDED
    if locked.remaining_cents < amount_cents:
        return Reason.BUDGET_EXCEEDED
    if locked.max_calls is not None and locked.calls_used >= locked.max_calls:
        return Reason.MAX_CALLS_EXCEEDED
    return Reason.BUDGET_EXCEEDED


class IdempotencyReplay(Exception):
    """Existing completed claim — caller should return the stored result."""

    def __init__(self, record: IdempotencyRecord):
        self.record = record


def _claim_idempotency(
    session: Session,
    *,
    principal_id: str,
    idempotency_key: str,
    request_hash: str | None,
) -> IdempotencyRecord:
    """Insert LogicalOperation claim (status=in_progress) or raise replay/conflict/in-progress.

    Unique (principal_id, key) is the concurrency gate: losers do not decrement budget.
    Completed claims replay. in_progress claims return IN_PROGRESS unless rolled back
    or explicitly released for retry.
    """
    rec = IdempotencyRecord(
        id=new_id(),
        principal_id=principal_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status="in_progress",
        result_json=None,
    )
    try:
        with session.begin_nested():
            session.add(rec)
            session.flush()
        return rec
    except IntegrityError:
        existing = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.principal_id == principal_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise PolicyDenied(Reason.IDEMPOTENCY_CONFLICT, "claim unique violation")
        stored = existing.request_hash
        if stored and request_hash and stored != request_hash:
            raise PolicyDenied(Reason.IDEMPOTENCY_CONFLICT, "same key, different request hash")
        if existing.status == "completed" and existing.result_json:
            raise IdempotencyReplay(existing)
        raise PolicyDenied(Reason.IN_PROGRESS, "wait-or-conflict")
