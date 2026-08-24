"""Stripe adapter. HTTP happens only in drain_outbox(), after the ledger commit."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crossing.models import Account, Outbox, Principal, StripeEvent, new_id, utcnow

STRIPE_API = "https://api.stripe.com/v1"
MAX_ATTEMPTS = 8
LEASE_SECONDS = 30
MICRO_PER_CENT = 1_000_000


def secret() -> str:
    return os.environ.get("STRIPE_SECRET_KEY") or ""


def webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET") or ""


def price_id() -> str:
    return os.environ.get("STRIPE_PRICE_ID") or ""


def configured() -> bool:
    return bool(secret())


def fee_bps() -> int:
    raw = os.environ.get("CROSSING_FEE_BPS") or "0"
    try:
        n = int(raw)
    except ValueError:
        return 0
    return max(0, n)


def fee_microcents_for(amount_cents: int, bps: int | None = None) -> int:
    """Integer microcents (1e-6 of a cent). Never float. 4bps of 1 cent stays 400 microcents."""
    b = fee_bps() if bps is None else int(bps)
    amt = int(amount_cents)
    if amt < 0 or b < 0:
        return 0
    # amount_cents * bps / 10000 cents * 1_000_000 microcents/cent = amount * bps * 100
    return amt * b * 100


def apply_platform_fee(account: Account, amount_cents: int) -> int:
    """Accumulate fee microcents; return newly invoiceable whole cents (>= 1 cent)."""
    add = fee_microcents_for(amount_cents)
    account.fee_microcents = int(account.fee_microcents or 0) + add
    invoice = account.fee_microcents // MICRO_PER_CENT
    account.fee_microcents = account.fee_microcents % MICRO_PER_CENT
    account.fee_invoiced_cents = int(account.fee_invoiced_cents or 0) + invoice
    return invoice


def _account_for_principal(session: Session, principal_id: str) -> Account | None:
    p = session.get(Principal, principal_id)
    if p is None:
        return None
    return session.get(Account, p.account_id)


def enqueue(
    session: Session,
    *,
    receipt_id: str,
    amount_cents: int,
    principal_id: str,
    customer_id: str | None = None,
) -> Outbox:
    """Insert a pending billing_outbox row. No HTTP. Unique per receipt_id."""
    existing = session.scalar(select(Outbox).where(Outbox.receipt_id == receipt_id))
    if existing is not None:
        return existing
    account = _account_for_principal(session, principal_id)
    if not customer_id:
        if account is not None and account.stripe_customer_id:
            customer_id = account.stripe_customer_id
        else:
            customer_id = os.environ.get("STRIPE_CUSTOMER_ID") or None
    payload = {
        "receipt_id": receipt_id,
        "amount_cents": int(amount_cents),
        "principal_id": principal_id,
        "customer_id": customer_id,
        "fee_bps": fee_bps(),
        "platform_fee_invoice_cents": 0,
    }
    row = Outbox(
        id=new_id(),
        kind="stripe_meter",
        payload_json=json.dumps(payload, sort_keys=True),
        status="pending",
        attempts=0,
        receipt_id=receipt_id,
        next_attempt_at=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Outbox).where(Outbox.receipt_id == receipt_id))
        if existing is not None:
            return existing
        raise
    if account is not None:
        invoice_cents = apply_platform_fee(account, amount_cents)
        payload["platform_fee_invoice_cents"] = int(invoice_cents)
        row.payload_json = json.dumps(payload, sort_keys=True)
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
        "payload[value]": str(max(0, int(payload.get("amount_cents") or 0))),
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


def backoff_minutes(attempts: int) -> int:
    """1, 2, 4, 8, ... minutes, cap 60."""
    n = max(1, int(attempts))
    return min(60, 2 ** (n - 1))


def lease_seconds() -> int:
    raw = os.environ.get("CROSSING_OUTBOX_LEASE_SECONDS") or str(LEASE_SECONDS)
    try:
        n = int(raw)
    except ValueError:
        n = LEASE_SECONDS
    return max(1, n)


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
    row.claimed_at = utcnow()
    session.flush()
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


def _claimable_clause(now, stale):
    due = or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= now)
    retryable = and_(
        Outbox.status.in_(("pending", "failed")),
        due,
        Outbox.attempts < MAX_ATTEMPTS,
    )
    reclaim = and_(
        Outbox.status == "sending",
        or_(Outbox.claimed_at.is_(None), Outbox.claimed_at <= stale),
    )
    return or_(retryable, reclaim)


def _claim_outbox_rows(session: Session, *, limit: int) -> list[Outbox]:
    """Atomically claim pending/retryable/stale-sending rows (status -> sending)."""
    now = utcnow()
    stale = now - timedelta(seconds=lease_seconds())
    clause = _claimable_clause(now, stale)
    ids = list(session.scalars(select(Outbox.id).where(clause).limit(limit)).all())
    claimed: list[Outbox] = []
    for oid in ids:
        result = session.execute(
            update(Outbox)
            .where(Outbox.id == oid, clause)
            .values(status="sending", claimed_at=now)
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


def verify_webhook_signature(
    payload: bytes,
    header: str | None,
    secret: str | None = None,
    *,
    tolerance: int = 300,
) -> bool:
    secret = secret if secret is not None else webhook_secret()
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        if k and v:
            parts.setdefault(k, []).append(v)
    ts_s = (parts.get("t") or [None])[0]
    v1s = parts.get("v1") or []
    if not ts_s or not v1s:
        return False
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > tolerance:
        return False
    signed = f"{ts_s}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in v1s)


def sign_webhook_payload(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Test helper: Stripe-compatible stripe-signature header. Never logs the secret."""
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode("utf-8") + payload
    v1 = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def _customer_from_event(event: dict[str, Any]) -> str | None:
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return None
    cus = obj.get("customer")
    if isinstance(cus, str) and cus:
        return cus
    if obj.get("object") == "customer" and isinstance(obj.get("id"), str):
        return obj.get("id")
    return None


def apply_stripe_event(session: Session, event: dict[str, Any]) -> Account | None:
    """Idempotent SET of control-plane billing fields. Never touches ledger remaining_cents."""
    customer_id = _customer_from_event(event)
    if not customer_id:
        return None
    account = session.scalar(select(Account).where(Account.stripe_customer_id == customer_id))
    if account is None:
        return None
    obj = (event.get("data") or {}).get("object") or {}
    etype = event.get("type") or ""
    if etype.startswith("customer.subscription") or etype in (
        "invoice.paid",
        "invoice.payment_succeeded",
        "checkout.session.completed",
    ):
        status = obj.get("status")
        if isinstance(status, str):
            account.stripe_status = status
        sub = obj.get("subscription")
        if obj.get("object") == "subscription":
            sub = obj.get("id")
        if isinstance(sub, str):
            account.stripe_subscription_id = sub
        price = None
        items = obj.get("items") or {}
        data = items.get("data") if isinstance(items, dict) else None
        if isinstance(data, list) and data:
            price_obj = (data[0] or {}).get("price") or {}
            if isinstance(price_obj, dict):
                price = price_obj.get("id")
        if not price:
            price = (obj.get("plan") or {}).get("id") if isinstance(obj.get("plan"), dict) else None
        env_price = price_id()
        if isinstance(price, str):
            account.stripe_price_id = price
        elif env_price:
            account.stripe_price_id = env_price
        session.flush()
    return account


def record_stripe_event(session: Session, event: dict[str, Any]) -> tuple[StripeEvent, bool]:
    """Insert event_id PK. Returns (row, inserted). Duplicate → (existing, False)."""
    event_id = event.get("id")
    if not event_id or not isinstance(event_id, str):
        raise ValueError("missing event id")
    existing = session.get(StripeEvent, event_id)
    if existing is not None:
        return existing, False
    row = StripeEvent(
        event_id=event_id,
        type=str(event.get("type") or ""),
        account_id=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.get(StripeEvent, event_id)
        if existing is not None:
            return existing, False
        raise
    account = apply_stripe_event(session, event)
    if account is not None:
        row.account_id = account.id
        session.flush()
    return row, True


def attach_stripe_customer(session: Session, account_id: str, stripe_customer_id: str) -> Account:
    acct = session.get(Account, account_id)
    if acct is None:
        raise KeyError(account_id)
    acct.stripe_customer_id = stripe_customer_id
    session.flush()
    return acct


def billing_status(session: Session, account_id: str) -> dict[str, Any]:
    from crossing.models import Outbox as OutboxModel
    from crossing.models import Receipt

    acct = session.get(Account, account_id)
    if acct is None:
        raise KeyError(account_id)
    principals = list(session.scalars(select(Principal).where(Principal.account_id == account_id)).all())
    pids = [p.id for p in principals]
    receipt_count = 0
    usage_cents = 0
    if pids:
        recs = list(session.scalars(select(Receipt).where(Receipt.principal_id.in_(pids))).all())
        receipt_count = len(recs)
        usage_cents = sum(int(r.amount_cents or 0) for r in recs)
    outbox_rows = list(session.scalars(select(OutboxModel)).all())
    # tenant-filter via payload principal_id
    pid_set = set(pids)
    mine = []
    for row in outbox_rows:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            continue
        if payload.get("principal_id") in pid_set:
            mine.append(row)
    pending = sum(1 for r in mine if r.status in ("pending", "failed", "sending"))
    sent = sum(1 for r in mine if r.status in ("sent", "noop"))
    plan = acct.stripe_price_id or price_id() or None
    return {
        "account_id": acct.id,
        "plan_price_id": plan,
        "stripe_customer_present": bool(acct.stripe_customer_id),
        "stripe_status": acct.stripe_status,
        "receipt_count": receipt_count,
        "usage_cents": usage_cents,
        "outbox_pending": pending,
        "outbox_sent": sent,
        "fee_bps": fee_bps(),
        "fee_microcents": int(acct.fee_microcents or 0),
        "fee_invoiced_cents": int(acct.fee_invoiced_cents or 0),
    }
