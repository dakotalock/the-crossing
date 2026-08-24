"""Append-only ledger and atomic budget reservations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    """Claim + reserve + invocation insert as one savepoint, then COMMIT.

    If reserve fails, the LogicalOperation claim rolls back with the savepoint.
    Deny events are recorded *outside* that unit so they persist without a
    poisoned in_progress claim.
    """
    try:
        with session.begin_nested():
            if idempotency_key:
                _claim_idempotency(
                    session,
                    principal_id=mandate.principal_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
            res = reserve(
                session,
                mandate,
                amount_cents,
                idempotency_key=idempotency_key,
                nonce=nonce,
                task_id=task_id,
                record_deny=False,
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
    except PolicyDenied as exc:
        session.refresh(mandate)
        if exc.reason in (Reason.BUDGET_EXCEEDED, Reason.MAX_CALLS_EXCEEDED):
            append_event(
                session,
                principal_id=mandate.principal_id,
                mandate_id=mandate.id,
                kind="deny",
                amount_cents=amount_cents,
                remaining_after=mandate.remaining_cents,
                idempotency_key=idempotency_key,
                note=exc.reason,
                task_id=task_id,
            )
        raise
    session.commit()
    return res, inv


def _clear_logical_operation(session: Session, invocation: Invocation) -> None:
    if not invocation.idempotency_key:
        return
    claim = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.principal_id == invocation.principal_id,
            IdempotencyRecord.idempotency_key == invocation.idempotency_key,
        )
    )
    if claim is not None:
        session.delete(claim)


class _FinalizeLost(Exception):
    """Internal: roll back a finalize savepoint because a CAS lost."""


@dataclass
class FinalizeResult:
    """Outcome of finalize_success / finalize_release."""

    won: bool
    reservation: Reservation | None = None
    invocation: Invocation | None = None
    receipt: Any = None
    outbox: Any = None


@dataclass
class MarkExecutingResult:
    """Outcome of reserved → executing CAS + COMMIT."""

    won: bool
    invocation: Invocation
    reason: str | None = None


def mark_executing(session: Session, invocation: Invocation) -> MarkExecutingResult:
    """CAS reserved → executing and COMMIT before any provider I/O.

    If 0 rows match, do not call the provider. A lost CAS means another
    worker already started dispatch, recovery already ran, or the attempt
    is terminal.
    """
    row = session.execute(
        update(Invocation)
        .where(Invocation.id == invocation.id, Invocation.status == "reserved")
        .values(status="executing")
        .returning(Invocation.id)
    ).first()
    if row is None:
        session.refresh(invocation)
        status = invocation.status
        if status == "executing":
            reason = Reason.IN_PROGRESS
        elif status in ("committed", "released", "ambiguous", "executed_fail"):
            reason = "FINALIZE_LOST"
        else:
            reason = Reason.IN_PROGRESS
        return MarkExecutingResult(won=False, invocation=invocation, reason=reason)
    session.refresh(invocation)
    session.commit()
    return MarkExecutingResult(won=True, invocation=invocation)


def mark_executed_fail(session: Session, invocation: Invocation) -> Invocation:
    """CAS executing → executed_fail and COMMIT. No refund, claim stays.

    Used when the provider raises after dispatch was durably started.
    Side-effect is uncertain; operator must reconcile.
    """
    row = session.execute(
        update(Invocation)
        .where(Invocation.id == invocation.id, Invocation.status == "executing")
        .values(status="executed_fail")
        .returning(Invocation.id)
    ).first()
    if row is None:
        session.refresh(invocation)
        return invocation
    session.refresh(invocation)
    session.commit()
    return invocation


def recover_reserved(session: Session, invocation: Invocation, *, mode: str = "ambiguous") -> Invocation:
    """Recover reserved or in-flight rows.

    ``reserved``: execution has definitely not been marked started.
    Default ``mode="ambiguous"`` marks ambiguous with **no** refund and **no**
    claim clear. Explicit ``mode="release"`` goes through ``finalize_release``
    (invocation must still be ``reserved``): reservation ``held→released`` and
    invocation ``reserved→released`` must both win or nothing is refunded.

    ``executing``: side-effect is uncertain. Never auto-refund, never clear
    the LogicalOperation. ``mode="release"`` refuses (row stays executing).
    Default ``mode="ambiguous"`` marks ambiguous without refund (already
    uncertain).

    Terminal (committed / released / ambiguous / executed_fail): leave alone.
    """
    if invocation.status in ("committed", "released", "ambiguous", "executed_fail"):
        return invocation
    if invocation.status == "executing":
        if mode == "release":
            return invocation
        invocation.status = "ambiguous"
        session.flush()
        return invocation
    if mode == "release":
        res = session.get(Reservation, invocation.reservation_id) if invocation.reservation_id else None
        fin = finalize_release(
            session,
            invocation,
            res,
            allowed_statuses=("reserved",),
        )
        return fin.invocation if fin.invocation is not None else invocation
    if invocation.status != "reserved":
        return invocation
    invocation.status = "ambiguous"
    session.flush()
    return invocation


def _cas_reservation(session: Session, reservation: Reservation, new_status: str) -> bool:
    row = session.execute(
        update(Reservation)
        .where(Reservation.id == reservation.id, Reservation.status == "held")
        .values(status=new_status)
        .returning(Reservation.id)
    ).first()
    if row is None:
        session.refresh(reservation)
        return False
    session.refresh(reservation)
    return True


def _cas_invocation(session: Session, invocation: Invocation, *, allowed: tuple[str, ...], new_status: str) -> bool:
    row = session.execute(
        update(Invocation)
        .where(Invocation.id == invocation.id, Invocation.status.in_(allowed))
        .values(status=new_status)
        .returning(Invocation.id)
    ).first()
    if row is None:
        session.refresh(invocation)
        return False
    session.refresh(invocation)
    return True


def _refund_mandate(session: Session, reservation: Reservation) -> int | None:
    refund = session.execute(
        update(Mandate)
        .where(Mandate.id == reservation.mandate_id)
        .values(
            remaining_cents=Mandate.remaining_cents + reservation.amount_cents,
            calls_used=case((Mandate.calls_used > 0, Mandate.calls_used - 1), else_=0),
        )
        .returning(Mandate.remaining_cents)
    ).first()
    remaining = int(refund[0]) if refund is not None else None
    if remaining is not None:
        session.refresh(session.get(Mandate, reservation.mandate_id))
    return remaining


def _complete_logical_operation(session: Session, invocation: Invocation, result_json: str | None) -> None:
    if not invocation.idempotency_key:
        return
    claim = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.principal_id == invocation.principal_id,
            IdempotencyRecord.idempotency_key == invocation.idempotency_key,
        )
    )
    payload = result_json
    if claim is None:
        session.add(
            IdempotencyRecord(
                id=new_id(),
                principal_id=invocation.principal_id,
                idempotency_key=invocation.idempotency_key,
                request_hash=invocation.request_hash,
                status="completed",
                result_json=payload,
            )
        )
    else:
        claim.status = "completed"
        if invocation.request_hash:
            claim.request_hash = invocation.request_hash
        claim.result_json = payload


def finalize_success(
    session: Session,
    reservation: Reservation,
    invocation: Invocation,
    *,
    receipt_fn: Callable[[Session], Any] | None = None,
    billing_fn: Callable[[Session, Any], Any] | None = None,
    result_json: str | None = None,
    result_fn: Callable[[Any], str | None] | None = None,
    commit_txn: bool = True,
) -> FinalizeResult:
    """CAS reservation held→committed; only the winner finalizes the logical op.

    One savepoint owns reservation, invocation, receipt, billing outbox, claim
    completion, and the commit ledger event. A lost CAS rolls the savepoint
    back: no receipt, no outbox, no invocation rewrite, no claim complete.
    """
    receipt = None
    outbox = None
    try:
        with session.begin_nested():
            if not _cas_reservation(session, reservation, "committed"):
                raise _FinalizeLost()
            if not _cas_invocation(
                session,
                invocation,
                allowed=("reserved", "executing", "executed_ok"),
                new_status="committed",
            ):
                raise _FinalizeLost()
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
                task_id=invocation.task_id,
            )
            if receipt_fn is not None:
                receipt = receipt_fn(session)
            if billing_fn is not None and receipt is not None:
                outbox = billing_fn(session, receipt)
            payload = result_json
            if result_fn is not None:
                payload = result_fn(receipt)
            _complete_logical_operation(session, invocation, payload)
    except _FinalizeLost:
        session.refresh(invocation)
        session.refresh(reservation)
        return FinalizeResult(won=False, reservation=reservation, invocation=invocation)
    if commit_txn:
        session.commit()
    else:
        session.flush()
    return FinalizeResult(
        won=True,
        reservation=reservation,
        invocation=invocation,
        receipt=receipt,
        outbox=outbox,
    )


def finalize_release(
    session: Session,
    invocation: Invocation,
    reservation: Reservation | None = None,
    *,
    allowed_statuses: tuple[str, ...] = ("reserved", "executed_fail"),
    commit_txn: bool = True,
) -> FinalizeResult:
    """CAS reservation held→released AND invocation allowed→released together.

    If either UPDATE matches 0 rows the savepoint rolls back: no refund, no
    claim delete, no invocation rewrite.
    """
    if reservation is None and invocation.reservation_id:
        reservation = session.get(Reservation, invocation.reservation_id)
    try:
        with session.begin_nested():
            if reservation is None:
                raise _FinalizeLost()
            if not _cas_reservation(session, reservation, "released"):
                raise _FinalizeLost()
            remaining = _refund_mandate(session, reservation)
            append_event(
                session,
                principal_id=reservation.principal_id,
                mandate_id=reservation.mandate_id,
                kind="release",
                amount_cents=reservation.amount_cents,
                remaining_after=remaining,
                reservation_id=reservation.id,
                idempotency_key=reservation.idempotency_key,
                task_id=invocation.task_id,
            )
            if not _cas_invocation(
                session,
                invocation,
                allowed=allowed_statuses,
                new_status="released",
            ):
                raise _FinalizeLost()
            _clear_logical_operation(session, invocation)
    except _FinalizeLost:
        session.refresh(invocation)
        if reservation is not None:
            session.refresh(reservation)
        return FinalizeResult(won=False, reservation=reservation, invocation=invocation)
    if commit_txn:
        session.commit()
    else:
        session.flush()
    return FinalizeResult(won=True, reservation=reservation, invocation=invocation)


def commit(session: Session, reservation: Reservation, *, task_id: str | None = None) -> Reservation:
    """Low-level CAS held -> committed. Loser is a no-op; only the winner writes the event.

    Callers that finalize a logical operation must use finalize_success().
    """
    if not _cas_reservation(session, reservation, "committed"):
        return reservation
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
    """Low-level CAS held -> released. Only the winner refunds remaining and writes the event.

    Callers that finalize a logical operation must use finalize_release().
    """
    if not _cas_reservation(session, reservation, "released"):
        return reservation
    remaining = _refund_mandate(session, reservation)
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
