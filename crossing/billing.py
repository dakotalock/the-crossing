"""Stripe adapter. No-ops without STRIPE_SECRET_KEY but always writes outbox rows."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from sqlalchemy.orm import Session

from crossing.models import Outbox, new_id

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
            row.status = "failed"
            row.last_error = str(result.get("error") or "stripe failed")
    except Exception as exc:  # noqa: BLE001 — adapter must never raise into commit
        row.status = "failed"
        row.last_error = exc.__class__.__name__
    session.flush()
    return row


def report_after_commit(session: Session, row: Outbox) -> Outbox:
    """Flush Stripe after ledger commit. Failures stay on the outbox row."""
    try:
        return flush_row(session, row)
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.last_error = exc.__class__.__name__
        session.flush()
        return row
