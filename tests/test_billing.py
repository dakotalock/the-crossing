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
