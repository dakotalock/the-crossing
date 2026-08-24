from __future__ import annotations

import httpx

from crossing import billing
from crossing.models import LedgerEvent, Outbox, Reservation


def test_stripe_adapter_failure_does_not_rollback_commit(seeded, monkeypatch):
    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_CUSTOMER_ID", "cus_x")

    def boom(_payload):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(billing, "post_stripe", boom)
    r = cx.invoke(m.id, "search", {"q": "bill"}, idempotency_key="bill-1")
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
        assert rows[0].status == "noop"



def test_outbox_failed_retryable_then_dead(seeded, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from crossing.models import LedgerEvent, Outbox, Reservation, utcnow

    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_CUSTOMER_ID", "cus_x")

    def boom(_payload):
        raise RuntimeError("http failed")

    monkeypatch.setattr(billing, "post_stripe", boom)
    r = cx.invoke(m.id, "search", {"q": "retry"}, idempotency_key="retry-1")
    assert r.ok is True
    assert cx.remaining(m.id) == 95

    with cx.session() as s:
        row = s.query(Outbox).one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        # due in the past -> retryable
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
