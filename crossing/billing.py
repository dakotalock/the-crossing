"""Stripe adapter. HTTP happens only in drain_outbox(), after the ledger commit."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from crossing.models import Outbox, new_id, utcnow

STRIPE_API = "https://api.stripe.com/v1"


def secret() -> str:
    return os.environ.get("STRIPE_SECRET_KEY") or ""


def configured() -> bool:
    return bool(secret())


def enqueue(
    session: Session,
    *,
    receipt_id: str,
    amount_cents: int,
    principal_id: str,
    customer_id: str | None = None,
) -> Outbox:
    """Insert a pending billing_outbox row. No HTTP."""
    payload = {
        "receipt_id": receipt_id,
        "amount_cents": amount_cents,
        "principal_id": principal_id,
        "customer_id": customer_id or os.environ.get("STRIPE_CUSTOMER_ID"),
    }
    row = Outbox(
        id=new_id(),
        kind="stripe_meter",
        payload_json=json.dumps(payload, sort_keys=True),
        status="pending",
        attempts=0,
    )
    session.add(row)
    session.flush()
    return row


def post_stripe(payload: dict[str, Any]) -> dict[str, Any]:
    key = secret()
    if not key:
        return {"ok": True, "noop": True, "stripe_reported": False}
    customer = payload.get("customer_id")
    if not customer:
        return {"ok": False, "stripe_reported": False, "error": "no stripe customer"}
    data = {
        "event_name": os.environ.get("STRIPE_METER_EVENT") or "crossing_usage",
        "identifier": payload.get("receipt_id"),
        "payload[stripe_customer_id]": customer,
        "payload[value]": str(max(1, int(payload.get("amount_cents") or 0))),
    }
    with httpx.Client(timeout=8.0) as client:
        resp = client.post(
            f"{STRIPE_API}/billing/meter_events",
            data=data,
            auth=(key, ""),
            headers={"Idempotency-Key": str(payload.get("receipt_id") or "")},
        )
    if resp.status_code >= 400:
        return {"ok": False, "stripe_reported": False, "error": f"http {resp.status_code}"}
    return {"ok": True, "stripe_reported": True}


MAX_ATTEMPTS = 8


def backoff_minutes(attempts: int) -> int:
    """1, 2, 4, 8, ... minutes, cap 60."""
    n = max(1, int(attempts))
    return min(60, 2 ** (n - 1))


def _mark_fail(row: Outbox, error: str) -> None:
    row.last_error = error
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "dead"
    else:
        row.status = "failed"
        row.next_attempt_at = utcnow() + timedelta(minutes=backoff_minutes(row.attempts))


def flush_row(session: Session, row: Outbox) -> Outbox:
    payload = json.loads(row.payload_json)
    row.attempts += 1
    try:
        result = post_stripe(payload)
        if result.get("noop"):
            row.status = "noop"
            row.last_error = None
        elif result.get("ok"):
            row.status = "sent"
            row.last_error = None
        else:
            _mark_fail(row, str(result.get("error") or "stripe failed"))
    except Exception as exc:  # noqa: BLE001 — adapter must never raise into commit
        _mark_fail(row, exc.__class__.__name__)
    session.flush()
    return row


def _claim_outbox_rows(session: Session, *, limit: int) -> list[Outbox]:
    """Atomically claim pending/retryable rows (status -> sending)."""
    now = utcnow()
    ids = list(
        session.scalars(
            select(Outbox.id).where(
                or_(
                    Outbox.status == "pending",
                    and_(
                        Outbox.status == "failed",
                        Outbox.next_attempt_at.is_not(None),
                        Outbox.next_attempt_at <= now,
                        Outbox.attempts < MAX_ATTEMPTS,
                    ),
                )
            ).limit(limit)
        ).all()
    )
    claimed: list[Outbox] = []
    for oid in ids:
        result = session.execute(
            update(Outbox)
            .where(Outbox.id == oid, Outbox.status.in_(("pending", "failed")))
            .values(status="sending")
        )
        if result.rowcount == 1:
            session.expire_all()
            row = session.get(Outbox, oid)
            if row is not None:
                claimed.append(row)
    session.flush()
    return claimed


def report_after_commit(session: Session, row: Outbox) -> Outbox:
    """Must NOT HTTP to Stripe. Outbox row is already pending; drain after COMMIT."""
    if row.status != "pending":
        row.status = "pending"
        session.flush()
    return row


def drain_outbox(session: Session | None = None, *, limit: int = 50) -> list[Outbox]:
    """Send pending billing_outbox rows. Stripe failure leaves ledger intact."""
    from crossing import db

    own = session is None
    if own:
        session = db.get_session()
    assert session is not None
    try:
        rows = _claim_outbox_rows(session, limit=limit)
        for row in rows:
            flush_row(session, row)
        session.commit()
        return rows
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()
