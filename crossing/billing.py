"""Stripe adapter. HTTP happens only in drain_outbox() / the worker, after COMMIT."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from crossing.models import Account, Outbox, Principal, StripeEvent, new_id, utcnow
from crossing.policy import PolicyDenied, Reason

log = logging.getLogger("crossing.billing")

UNPAID_STATUSES = frozenset({"unpaid", "canceled", "cancelled", "incomplete_expired"})

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


def warn_unsent_fees() -> None:
    """Fees are accumulated locally; Stripe has no fee line until one is wired."""
    if fee_bps() != 0:
        log.warning(
            "CROSSING_FEE_BPS is non-zero but post_stripe does not send a fee line; "
            "keep CROSSING_FEE_BPS=0 until a Stripe invoice item exists. "
            "Microcents accumulate on the account only."
        )


def is_billable(account: Account | None) -> bool:
    """When STRIPE_SECRET_KEY is set, paid work requires a live customer on the account."""
    if not configured():
        return True
    if account is None:
        return False
    if not (account.stripe_customer_id or "").strip():
        return False
    st = (account.stripe_status or "").strip().lower()
    if st in UNPAID_STATUSES:
        return False
    return True


def require_billable(session: Session, principal_id: str) -> Account | None:
    account = _account_for_principal(session, principal_id)
    if not is_billable(account):
        raise PolicyDenied(Reason.BILLING_REQUIRED, "stripe customer required for paid work")
    return account


def fee_microcents_for(amount_cents: int, bps: int | None = None) -> int:
    """Integer microcents (1e-6 of a cent). Never float. 4bps of 1 cent stays 400 microcents."""
    b = fee_bps() if bps is None else int(bps)
    amt = int(amount_cents)
    if amt < 0 or b < 0:
        return 0
    # amount_cents * bps / 10000 cents * 1_000_000 microcents/cent = amount * bps * 100
    return amt * b * 100


def apply_platform_fee(account: Account, amount_cents: int) -> int:
    """Accumulate integer fee microcents only. Never claim invoiced cents Stripe did not receive.

    Returns 0 always (no Stripe fee line). fee_invoiced_cents is not mutated.
    """
    add = int(fee_microcents_for(amount_cents))
    if add <= 0:
        return 0
    sess = object_session(account)
    if sess is not None and account.id:
        sess.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(fee_microcents=Account.fee_microcents + add)
        )
        sess.flush()
        sess.refresh(account)
        return 0
    account.fee_microcents = int(account.fee_microcents or 0) + add
    return 0


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
            customer_id = None
    payload = {
        "receipt_id": receipt_id,
        "amount_cents": int(amount_cents),
        "principal_id": principal_id,
        "customer_id": customer_id,
        "fee_bps": fee_bps(),
        # Never claim invoiced fees that were not sent to Stripe.
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


def _terminal_values(attempts_after: int, result: dict[str, Any]) -> dict[str, Any]:
    """Compute sent|noop|failed|dead fields after one Stripe attempt."""
    now = utcnow()
    values: dict[str, Any] = {"attempts": attempts_after, "claimed_at": now}
    if result.get("noop"):
        values.update(status="noop", last_error=None, next_attempt_at=None)
        return values
    if result.get("ok"):
        values.update(status="sent", last_error=None, next_attempt_at=None)
        return values
    error = str(result.get("error") or "stripe failed")
    if attempts_after >= MAX_ATTEMPTS:
        values.update(status="dead", last_error=error, next_attempt_at=None)
    else:
        values.update(
            status="failed",
            last_error=error,
            next_attempt_at=now + timedelta(minutes=backoff_minutes(attempts_after)),
        )
    return values


def _cas_terminal_from_sending(session: Session, oid: str, result: dict[str, Any]) -> Outbox | None:
    """UPDATE billing_outbox SET status=terminal WHERE id=:id AND status='sending' RETURNING.

    Loses (returns None) if a sibling worker already moved the row off sending.
    """
    row = session.get(Outbox, oid)
    if row is None or row.status != "sending":
        return None
    values = _terminal_values(int(row.attempts or 0) + 1, result)
    ret = session.execute(
        update(Outbox)
        .where(Outbox.id == oid, Outbox.status == "sending")
        .values(**values)
        .returning(Outbox.id)
    ).first()
    if ret is None:
        session.refresh(row)
        return None
    session.expire_all()
    return session.get(Outbox, oid)


def flush_row(session: Session, row: Outbox) -> Outbox:
    """HTTP for an already-claimed sending row. Caller must have COMMITTED sending first."""
    payload = json.loads(row.payload_json)
    try:
        result = post_stripe(payload)
    except Exception as exc:  # noqa: BLE001 — adapter must never raise into commit
        result = {"ok": False, "stripe_reported": False, "error": exc.__class__.__name__}
    cas = _cas_terminal_from_sending(session, row.id, result)
    if cas is None:
        session.refresh(row)
        return row
    session.flush()
    return cas


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
    """Claim sending + COMMIT, then HTTP, then CAS sending→terminal + COMMIT.

    Never HTTP in the same uncommitted transaction as the first claim.
    Stripe Idempotency-Key is receipt_id so a crash after HTTP before sent is retried safely.
    """
    from crossing import db

    own = session is None
    if own:
        session = db.get_session()
    assert session is not None
    processed: list[Outbox] = []
    try:
        claimed = _claim_outbox_rows(session, limit=limit)
        jobs = [(row.id, json.loads(row.payload_json)) for row in claimed]
        session.commit()
        for oid, payload in jobs:
            try:
                result = post_stripe(payload)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "stripe_reported": False, "error": exc.__class__.__name__}
            cas = _cas_terminal_from_sending(session, oid, result)
            if cas is None:
                session.rollback()
                continue
            session.commit()
            session.refresh(cas)
            processed.append(cas)
        return processed
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()


def requeue_dead(session: Session, outbox_id: str | None = None) -> list[Outbox]:
    """Admin: dead → pending with attempts=0 so claim (attempts < MAX) can drain again."""
    q = select(Outbox).where(Outbox.status == "dead")
    if outbox_id:
        q = q.where(Outbox.id == outbox_id)
    rows = list(session.scalars(q).all())
    now = utcnow()
    out: list[Outbox] = []
    for row in rows:
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = now
        row.claimed_at = None
        out.append(row)
    session.flush()
    return out


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
    try:
        with session.begin_nested():
            acct.stripe_customer_id = stripe_customer_id
            session.flush()
    except IntegrityError as exc:
        raise PolicyDenied(
            Reason.UNAUTHORIZED,
            "stripe_customer_id already bound to another account",
        ) from exc
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
    dead = sum(1 for r in mine if r.status == "dead")
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
        "outbox_dead": dead,
        "fee_bps": fee_bps(),
        "fee_microcents": int(acct.fee_microcents or 0),
        "fee_invoiced_cents": int(acct.fee_invoiced_cents or 0),
    }
