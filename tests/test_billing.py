from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from crossing import billing
from crossing.models import Account, LedgerEvent, Outbox, Principal, Reservation, StripeEvent, utcnow


def _attach_customer(cx, principal, cus="cus_x"):
    with cx.session() as s:
        acct = s.get(Account, s.get(Principal, principal.id).account_id)
        acct.stripe_customer_id = cus


def test_stripe_adapter_failure_does_not_rollback_commit(seeded, monkeypatch):
    cx, p, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    _attach_customer(cx, p)

    def boom(_payload):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(billing, "post_stripe", boom)
    r = cx.invoke(m.id, "search", {"q": "bill"}, idempotency_key="bill-1")
    billing.drain_outbox()
    assert r.ok is True
    assert cx.remaining(m.id) == 95
    with cx.session() as s:
        holds = s.query(Reservation).all()
        assert any(h.status == "committed" for h in holds)
        commits = [e for e in s.query(LedgerEvent).all() if e.kind == "commit"]
        assert commits
        rows = s.query(Outbox).all()
        assert rows
        assert rows[0].status == "failed"
        assert rows[0].last_error


def test_stripe_noop_without_key(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "n"}, idempotency_key="noop-1")
    assert r.ok
    with cx.session() as s:
        rows = s.query(Outbox).all()
        assert rows[0].status == "pending"
    billing.drain_outbox()
    with cx.session() as s:
        rows = s.query(Outbox).all()
        assert rows[0].status == "noop"


def test_outbox_failed_retryable_then_dead(seeded, monkeypatch):
    cx, p, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    _attach_customer(cx, p)

    def boom(_payload):
        raise RuntimeError("http failed")

    monkeypatch.setattr(billing, "post_stripe", boom)
    r = cx.invoke(m.id, "search", {"q": "retry"}, idempotency_key="retry-1")
    billing.drain_outbox()
    assert r.ok is True
    assert cx.remaining(m.id) == 95

    with cx.session() as s:
        row = s.query(Outbox).one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        rid = row.id

    monkeypatch.setattr(billing, "post_stripe", boom)
    billing.drain_outbox()
    with cx.session() as s:
        row = s.get(Outbox, rid)
        assert row.status == "failed"
        assert row.attempts == 2
        assert any(e.kind == "commit" for e in s.query(LedgerEvent).all())
        assert any(h.status == "committed" for h in s.query(Reservation).all())

    with cx.session() as s:
        row = s.get(Outbox, rid)
        row.attempts = billing.MAX_ATTEMPTS - 1
        row.status = "failed"
        row.next_attempt_at = utcnow() - timedelta(seconds=1)

    billing.drain_outbox()
    with cx.session() as s:
        row = s.get(Outbox, rid)
        assert row.status == "dead"
        assert row.attempts == billing.MAX_ATTEMPTS
        assert any(e.kind == "commit" for e in s.query(LedgerEvent).all())
    assert cx.remaining(m.id) == 95


def test_drain_not_inside_ledger_txn(seeded, monkeypatch):
    cx, _, _, m = seeded
    calls = {"n": 0}

    def track(payload):
        calls["n"] += 1
        return {"ok": True, "noop": True, "stripe_reported": False}

    monkeypatch.setattr(billing, "post_stripe", track)
    orig_enqueue = billing.enqueue

    def enqueue_no_http(session, **kwargs):
        assert calls["n"] == 0
        return orig_enqueue(session, **kwargs)

    monkeypatch.setattr(billing, "enqueue", enqueue_no_http)
    r = cx.invoke(m.id, "search", {"q": "after"}, idempotency_key="after-1")
    assert r.ok
    assert calls["n"] == 0
    billing.drain_outbox()
    assert calls["n"] >= 1
    with cx.session() as s:
        assert any(e.kind == "commit" for e in s.query(LedgerEvent).all())


def test_unique_outbox_per_receipt(seeded):
    cx, p, _, _ = seeded
    with cx.session() as s:
        a = billing.enqueue(s, receipt_id="rec-dup", amount_cents=5, principal_id=p.id)
        b = billing.enqueue(s, receipt_id="rec-dup", amount_cents=5, principal_id=p.id)
        assert a.id == b.id
        assert s.query(Outbox).count() == 1


def test_fee_bps_does_not_round_subcent_to_zero(seeded, monkeypatch):
    cx, p, _, _ = seeded
    monkeypatch.setenv("CROSSING_FEE_BPS", "4")
    assert billing.fee_microcents_for(5, 4) == 5 * 4 * 100
    assert billing.fee_microcents_for(5, 4) > 0
    with cx.session() as s:
        billing.enqueue(s, receipt_id="fee-1", amount_cents=5, principal_id=p.id)
        acct = s.get(Account, s.get(Principal, p.id).account_id)
        assert acct.fee_microcents == 2000
        assert acct.fee_invoiced_cents == 0
        for i in range(499):
            billing.apply_platform_fee(acct, 5)
        assert acct.fee_invoiced_cents == 0
        assert acct.fee_microcents == 2000 * 500


def test_ledger_does_not_store_commercial_price_id(seeded, monkeypatch):
    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_commercial_hardcode")
    r = cx.invoke(m.id, "search", {"q": "price"}, idempotency_key="price-1")
    assert r.ok
    with cx.session() as s:
        events = s.query(LedgerEvent).all()
        for e in events:
            assert e.amount_cents in (0, 5) or e.kind in ("deny",)
            blob = (e.note or "") + (e.idempotency_key or "")
            assert "price_commercial_hardcode" not in blob
            assert "price_" not in blob


def test_two_workers_claim_same_row_one_send(seeded, monkeypatch):
    cx, p, _, _ = seeded
    sent: list[str] = []
    lock = threading.Lock()

    def slow_post(payload):
        time.sleep(0.15)
        with lock:
            sent.append(payload["receipt_id"])
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(billing, "post_stripe", slow_post)
    with cx.session() as s:
        billing.enqueue(s, receipt_id="one-receipt", amount_cents=5, principal_id=p.id)

    errors = []

    def worker():
        try:
            billing.drain_outbox()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sent == ["one-receipt"]
    with cx.session() as s:
        row = s.query(Outbox).one()
        assert row.status == "sent"
        assert row.receipt_id == "one-receipt"


def test_crash_in_sending_then_reclaim(seeded, monkeypatch):
    cx, p, _, _ = seeded
    calls = {"n": 0}

    def ok_post(_payload):
        calls["n"] += 1
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(billing, "post_stripe", ok_post)
    with cx.session() as s:
        row = billing.enqueue(s, receipt_id="crash-1", amount_cents=5, principal_id=p.id)
        row.status = "sending"
        row.claimed_at = utcnow() - timedelta(seconds=billing.LEASE_SECONDS + 5)
        rid = row.id
    billing.drain_outbox()
    assert calls["n"] == 1
    with cx.session() as s:
        row = s.get(Outbox, rid)
        assert row.status == "sent"


def test_fresh_sending_lease_not_reclaimed(seeded, monkeypatch):
    cx, p, _, _ = seeded
    calls = {"n": 0}

    def ok_post(_payload):
        calls["n"] += 1
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(billing, "post_stripe", ok_post)
    with cx.session() as s:
        row = billing.enqueue(s, receipt_id="lease-fresh", amount_cents=5, principal_id=p.id)
        row.status = "sending"
        row.claimed_at = utcnow()
    billing.drain_outbox()
    assert calls["n"] == 0
    with cx.session() as s:
        row = s.query(Outbox).one()
        assert row.status == "sending"


def test_worker_once_drains(seeded, monkeypatch):
    from crossing.worker import run_once

    cx, p, _, _ = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setattr(billing, "post_stripe", lambda _p: {"ok": True, "stripe_reported": True})
    with cx.session() as s:
        billing.enqueue(s, receipt_id="once-1", amount_cents=5, principal_id=p.id)
    n = run_once()
    assert n == 1
    with cx.session() as s:
        assert s.query(Outbox).one().status == "sent"


def test_webhook_replay_does_not_duplicate(seeded, monkeypatch):
    from crossing.api import app
    from crossing.models import LedgerEvent as LE

    cx, p, _, _ = seeded
    secret = "whsec_test_replay"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", secret)
    with cx.session() as s:
        acct = s.get(Account, s.get(Principal, p.id).account_id)
        acct.stripe_customer_id = "cus_replay"
        aid = acct.id
        before = s.query(LE).count()
        remaining_notes = [(e.kind, e.amount_cents) for e in s.query(LE).all()]

    event = {
        "id": "evt_replay_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "object": "subscription",
                "id": "sub_1",
                "customer": "cus_replay",
                "status": "active",
                "items": {"data": [{"price": {"id": "price_test_plan"}}]},
            }
        },
    }
    raw = json.dumps(event, separators=(",", ":")).encode("utf-8")
    sig = billing.sign_webhook_payload(raw, secret)
    client = TestClient(app)
    first = client.post("/v1/stripe/webhooks", content=raw, headers={"stripe-signature": sig})
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    second = client.post("/v1/stripe/webhooks", content=raw, headers={"stripe-signature": sig})
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    with cx.session() as s:
        assert s.query(StripeEvent).count() == 1
        assert s.get(StripeEvent, "evt_replay_1") is not None
        assert s.query(LE).count() == before
        after = [(e.kind, e.amount_cents) for e in s.query(LE).all()]
        assert after == remaining_notes
        acct = s.get(Account, aid)
        assert acct.stripe_status == "active"
        assert acct.stripe_price_id == "price_test_plan"
        assert acct.remaining_cents if hasattr(acct, "remaining_cents") else True


def test_webhook_bad_signature_400(cx, monkeypatch):
    from crossing.api import app

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_other")
    raw = b'{"id":"evt_x","type":"ping"}'
    client = TestClient(app)
    r = client.post("/v1/stripe/webhooks", content=raw, headers={"stripe-signature": "t=1,v1=dead"})
    assert r.status_code == 400
    with cx.session() as s:
        assert s.query(StripeEvent).count() == 0


def test_billing_status_and_admin_attach(cx, monkeypatch):
    from crossing import auth
    from crossing.api import app
    from crossing.identity import create_principal

    monkeypatch.setenv("STRIPE_PRICE_ID", "price_plan_env")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_must_not_leak")
    monkeypatch.setenv("CROSSING_FEE_BPS", "4")
    client = TestClient(app)
    with cx.session() as s:
        p = create_principal(s, "Bill")
        issued = auth.issue_api_key(s, account_id=p.account_id, kind="customer")
        admin = auth.issue_api_key(s, account_id=p.account_id, kind="admin")
        pid, acct, secret, admin_secret = p.id, p.account_id, issued.secret, admin.secret
    attach = client.post(
        f"/v1/admin/accounts/{acct}/stripe-customer",
        json={"stripe_customer_id": "cus_status"},
        headers={"X-API-Key": admin_secret},
    )
    assert attach.status_code == 200
    assert attach.json()["stripe_customer_present"] is True
    st = client.get("/v1/billing/status", headers={"X-API-Key": secret})
    assert st.status_code == 200
    body = st.json()
    blob = json.dumps(body)
    assert "sk_test_must_not_leak" not in blob
    assert "STRIPE_SECRET_KEY" not in blob
    assert body["stripe_customer_present"] is True
    assert body["plan_price_id"] == "price_plan_env"
    assert body["fee_bps"] == 4
    assert "receipt_count" in body
    customer_attach = client.post(
        f"/v1/admin/accounts/{acct}/stripe-customer",
        json={"stripe_customer_id": "cus_nope"},
        headers={"X-API-Key": secret},
    )
    assert customer_attach.status_code in (403, 401, 404)
