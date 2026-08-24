from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crossing import billing, crypto, db, ledger
from crossing.api import app
from crossing.mandate import load_live_mandate
from crossing.models import Invocation, LedgerEvent, Mandate, Outbox, Reservation
from crossing.policy import PolicyDenied, Reason


def test_negative_child_budget_does_not_mint_money(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    before = cx.remaining(m.id)
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(m.id, child.id, spend_limit_cents=-10000, tools=["search"], servers=["mock"])
    assert ei.value.reason in (Reason.INVALID_AMOUNT, Reason.CHILD_SPEND_ESCALATION)
    assert cx.remaining(m.id) == before == 100


def test_reserve_and_commit_then_crash_keeps_decrement(seeded):
    cx, _, _, m = seeded
    with pytest.raises(RuntimeError):
        with cx.session() as s:
            mandate = load_live_mandate(s, m.id)
            ledger.reserve_and_commit(s, mandate, 5, idempotency_key="crash-1", tool="search", server="mock")
            raise RuntimeError("simulated crash before execute")
    # session_scope rolls back the empty post-commit txn; reserve already committed
    assert cx.remaining(m.id) == 95
    r = cx.invoke(m.id, "search", {"q": "after-crash"}, idempotency_key="crash-2")
    assert r.ok
    assert r.remaining_cents == 90
    with cx.session() as s:
        invs = s.query(Invocation).all()
        assert any(i.status == "reserved" for i in invs)


def test_stripe_not_called_until_after_commit(seeded, monkeypatch):
    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_CUSTOMER_ID", "cus_x")
    order: list[str] = []
    from sqlalchemy.orm import Session as SASession

    orig_commit = SASession.commit

    def wrapping(self, *args, **kwargs):
        order.append("commit")
        return orig_commit(self, *args, **kwargs)

    def spy(payload):
        order.append("stripe")
        return {"ok": True, "noop": True, "stripe_reported": False}

    monkeypatch.setattr(SASession, "commit", wrapping)
    monkeypatch.setattr(billing, "post_stripe", spy)
    r = cx.invoke(m.id, "search", {"q": "order"}, idempotency_key="order-1")
    assert r.ok
    assert "stripe" in order
    assert "commit" in order
    assert order.index("stripe") > order.index("commit")
    with cx.session() as s:
        assert any(e.kind == "commit" for e in s.query(LedgerEvent).all())
        assert s.query(Outbox).count() >= 1


def test_drain_http_failure_leaves_ledger(seeded, monkeypatch):
    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_CUSTOMER_ID", "cus_x")

    def boom(_payload):
        raise RuntimeError("http failed")

    monkeypatch.setattr(billing, "post_stripe", boom)
    r = cx.invoke(m.id, "search", {"q": "drain-fail"}, idempotency_key="drain-fail")
    assert r.ok is True
    assert cx.remaining(m.id) == 95
    with cx.session() as s:
        assert any(h.status == "committed" for h in s.query(Reservation).all())
        assert any(e.kind == "commit" for e in s.query(LedgerEvent).all())
        rows = s.query(Outbox).all()
        assert rows and rows[0].status == "failed"


def test_tampered_max_call_column_denied(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        row = s.get(Mandate, m.id)
        row.max_call_cents = 1
    r = cx.invoke(m.id, "search", {"q": "tamper"})
    assert r.ok is False
    assert r.reason == Reason.SIGNED_STATE_DIVERGED


def test_cross_principal_agent_rejected(cx):
    p1 = cx.create_principal("A")
    p2 = cx.create_principal("B")
    a2 = cx.create_agent(p2.id, "spy")
    with pytest.raises(PolicyDenied) as ei:
        cx.issue_mandate(p1.id, a2.id, 50, tools=["search"], servers=["mock"])
    assert ei.value.reason == Reason.AGENT_PRINCIPAL_MISMATCH


def test_unrelated_agent_cannot_inherit_parent_mandate(seeded):
    cx, p, a, m = seeded
    stranger = cx.create_agent(p.id, "stranger")
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(m.id, stranger.id, 10, tools=["search"], servers=["mock"])
    assert ei.value.reason == Reason.CHILD_AGENT_NOT_DESCENDANT


def test_http_deny_leaves_ledger_row(cx):
    client = TestClient(app)
    p = client.post("/v1/principals", json={"name": "Bob"}).json()
    a = client.post("/v1/agents", json={"principal_id": p["id"], "name": "bot"}).json()
    m = client.post(
        "/v1/mandates",
        json={
            "principal_id": p["id"],
            "agent_id": a["id"],
            "spend_limit_cents": 100,
            "tools": ["search"],
            "servers": ["mock"],
        },
    ).json()
    denied = client.post("/v1/invoke", json={"mandate_id": m["id"], "tool": "purchase", "arguments": {}})
    assert denied.status_code == 403
    with cx.session() as s:
        events = [e for e in s.query(LedgerEvent).all() if e.kind == "deny"]
        assert events


def test_idempotency_conflict_different_hash(seeded):
    cx, _, _, m = seeded
    a = cx.invoke(m.id, "search", {"q": "one"}, idempotency_key="same-key")
    assert a.ok
    b = cx.invoke(m.id, "search", {"q": "two"}, idempotency_key="same-key")
    assert b.ok is False
    assert b.reason == Reason.IDEMPOTENCY_CONFLICT


def test_receipt_default_has_hashes_not_result(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "hash-only"}, idempotency_key="hash-1")
    assert r.ok
    body = r.receipt["body"]
    assert "request_hash" in body and "response_hash" in body
    assert "result" not in body
    assert body.get("agent_id")
    assert body.get("task_id")
    assert r.task_id == body["task_id"]


def test_api_unauthenticated_mint_forbidden_without_dev(cx, monkeypatch):
    monkeypatch.setenv("CROSSING_ALLOW_DEV", "0")
    monkeypatch.setenv("CROSSING_API_KEY", "unit-test-key")
    monkeypatch.setenv("CROSSING_ED25519_SEED", "ab" * 32)
    crypto.reset_for_tests()
    client = TestClient(app)
    denied = client.post("/v1/principals", json={"name": "X"})
    assert denied.status_code == 401
    ok = client.post("/v1/principals", json={"name": "X"}, headers={"X-API-Key": "unit-test-key"})
    assert ok.status_code == 200
    monkeypatch.setenv("CROSSING_ALLOW_DEV", "1")
    crypto.reset_for_tests()



def test_recover_reserved_default_ambiguous_does_not_refund(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        _res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="amb-1", tool="search", server="mock"
        )
        inv_id = inv.id
    assert cx.remaining(m.id) == 95
    with cx.session() as s:
        inv = s.get(Invocation, inv_id)
        out = ledger.recover_reserved(s, inv)
        assert out.status == "ambiguous"
    from crossing.models import Mandate as MandateModel

    with cx.session() as s:
        inv = s.get(Invocation, inv_id)
        assert inv.status == "ambiguous"
        assert s.get(MandateModel, m.id).remaining_cents == 95
        held = s.query(Reservation).all()
        assert any(h.status == "held" for h in held)


def test_recover_reserved_release_refunds_only_when_explicit(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        _res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="rel-1", tool="search", server="mock"
        )
        inv_id = inv.id
    with cx.session() as s:
        inv = s.get(Invocation, inv_id)
        out = ledger.recover_reserved(s, inv, mode="release")
        assert out.status == "released"
    assert cx.remaining(m.id) == 100
