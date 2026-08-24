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
