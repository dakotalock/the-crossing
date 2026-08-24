from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crossing import billing, crypto, db, ledger
from crossing.api import app
from crossing.mandate import load_live_mandate
from crossing.models import IdempotencyRecord, Invocation, LedgerEvent, Mandate, Outbox, Reservation
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
    client.headers["X-API-Key"] = "dev"
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
    monkeypatch.setenv("CROSSING_ED25519_SEED", "ab" * 32)
    monkeypatch.setenv("CROSSING_KEY_PEPPER", "unit-test-pepper")
    crypto.reset_for_tests()
    from crossing import auth

    with db.session_scope() as s:
        from crossing.models import Account

        acct = s.query(Account).first()
        issued = auth.issue_api_key(s, account_id=acct.id, kind="admin", scopes=list(auth.ADMIN_SCOPES))
        secret = issued.secret
        key_id = issued.record.id
    client = TestClient(app)
    denied = client.post("/v1/principals", json={"name": "X"})
    assert denied.status_code == 401
    ok = client.post("/v1/principals", json={"name": "X"}, headers={"X-API-Key": secret})
    assert ok.status_code == 200
    # raw secret is not stored
    with db.session_scope() as s:
        from crossing.models import ApiKey

        row = s.get(ApiKey, key_id)
        assert row is not None
        assert secret not in (row.secret_hash or "")
        assert row.secret_hash != secret
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
        claim = s.query(IdempotencyRecord).filter_by(idempotency_key="amb-1").one_or_none()
        assert claim is not None
        assert claim.status == "in_progress"
    retry = cx.invoke(m.id, "search", {"q": "after-amb"}, idempotency_key="amb-1")
    assert retry.ok is False
    assert retry.reason == Reason.IN_PROGRESS
    assert retry.replayed is False
    assert cx.remaining(m.id) == 95


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


def test_budget_deny_does_not_poison_idempotency_key(seeded):
    """reserve fail must not leave a LogicalOperation claim in_progress."""
    cx, _, _, m = seeded
    with cx.session() as s:
        s.get(Mandate, m.id).remaining_cents = 0
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        with pytest.raises(PolicyDenied) as ei:
            ledger.reserve_and_commit(
                s, mandate, 5, idempotency_key="poison-key", tool="search", server="mock"
            )
        assert ei.value.reason == Reason.BUDGET_EXCEEDED
    with cx.session() as s:
        claims = s.query(IdempotencyRecord).filter_by(idempotency_key="poison-key").all()
        assert claims == []
        assert not any(c.status == "in_progress" for c in s.query(IdempotencyRecord).all())
        s.get(Mandate, m.id).remaining_cents = 100
    second = cx.invoke(m.id, "search", {"q": "funded"}, idempotency_key="poison-key")
    assert second.ok
    assert second.replayed is False


def test_recover_reserved_release_allows_retry_same_key(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        _res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="rel-retry", tool="search", server="mock"
        )
        inv_id = inv.id
    with cx.session() as s:
        out = ledger.recover_reserved(s, s.get(Invocation, inv_id), mode="release")
        assert out.status == "released"
        assert s.query(IdempotencyRecord).filter_by(idempotency_key="rel-retry").count() == 0
    assert cx.remaining(m.id) == 100
    second = cx.invoke(m.id, "search", {"q": "retry"}, idempotency_key="rel-retry")
    assert second.ok
    assert second.replayed is False
    with cx.session() as s:
        attempts = s.query(Invocation).filter_by(idempotency_key="rel-retry").all()
        assert len(attempts) == 2
        statuses = {i.status for i in attempts}
        assert "released" in statuses
        assert "committed" in statuses


def test_atomic_debit_refuses_overspend(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        with pytest.raises(PolicyDenied) as ei:
            ledger.reserve(s, mandate, 101)
        assert ei.value.reason == Reason.BUDGET_EXCEEDED
        s.refresh(mandate)
        assert mandate.remaining_cents == 100
        assert mandate.calls_used == 0



def test_mark_executing_commits_before_provider(seeded, monkeypatch):
    """Provider sees a durable executing row; no I/O if mark_executing did not commit."""
    from crossing import mock_mcp
    from crossing.models import Invocation

    cx, _, _, m = seeded
    seen: dict[str, str] = {}
    provider_calls: list[int] = []

    def hook(_args, **ids):
        provider_calls.append(1)
        with db.session_scope() as s2:
            inv = s2.get(Invocation, ids["invocation_id"])
            seen["status"] = inv.status if inv is not None else "missing"
        return {"hits": [{"title": "ok"}]}

    mock_mcp.HOOKS["search"] = hook
    try:
        r = cx.invoke(m.id, "search", {"q": "exec"}, idempotency_key="exec-before")
        assert r.ok
        assert seen["status"] == "executing"
        assert provider_calls == [1]
    finally:
        mock_mcp.HOOKS.clear()

    lost_calls: list[int] = []

    def lose(_session, invocation):
        return ledger.MarkExecutingResult(won=False, invocation=invocation, reason="IN_PROGRESS")

    def would_call(_args, **_ids):
        lost_calls.append(1)
        return {"hits": []}

    monkeypatch.setattr(ledger, "mark_executing", lose)
    mock_mcp.HOOKS["search"] = would_call
    try:
        r2 = cx.invoke(m.id, "search", {"q": "no-dispatch"}, idempotency_key="exec-lost")
        assert r2.ok is False
        assert r2.reason == "IN_PROGRESS"
        assert lost_calls == []
    finally:
        mock_mcp.HOOKS.clear()


def test_recovery_cannot_release_in_flight_execution(seeded):
    """recover_reserved(mode=release) must not refund an executing dispatch."""
    import threading

    from crossing import mock_mcp
    from crossing.models import IdempotencyRecord, Invocation, Mandate, Reservation

    cx, _, _, m = seeded
    dispatch_visible = threading.Event()
    allow_finish = threading.Event()
    inv_box: dict[str, str] = {}
    result_box: dict = {}

    def hook(_args, **ids):
        inv_box["id"] = ids["invocation_id"]
        dispatch_visible.set()
        if not allow_finish.wait(timeout=10):
            raise RuntimeError("provider not unblocked")
        return {"hits": [{"title": "late"}]}

    mock_mcp.HOOKS["search"] = hook
    try:
        def runner() -> None:
            result_box["r"] = cx.invoke(m.id, "search", {"q": "inflight"}, idempotency_key="inflight-1")

        t = threading.Thread(target=runner)
        t.start()
        assert dispatch_visible.wait(timeout=5)

        with cx.session() as s:
            inv = s.get(Invocation, inv_box["id"])
            assert inv is not None
            assert inv.status == "executing"
            out = ledger.recover_reserved(s, inv, mode="release")
            assert out.status in ("executing", "executed_fail")
            assert out.status != "released"

        assert cx.remaining(m.id) == 95
        with cx.session() as s:
            inv = s.get(Invocation, inv_box["id"])
            assert inv.status in ("executing", "executed_fail")
            assert inv.status != "released"
            hold = s.get(Reservation, inv.reservation_id)
            assert hold.status == "held"
            claim = s.query(IdempotencyRecord).filter_by(idempotency_key="inflight-1").one()
            assert claim.status == "in_progress"
            assert s.get(Mandate, m.id).remaining_cents == 95

        allow_finish.set()
        t.join(timeout=10)
        assert result_box["r"].ok
        assert cx.remaining(m.id) == 95
        with cx.session() as s:
            inv = s.get(Invocation, inv_box["id"])
            assert inv.status == "committed"
    finally:
        allow_finish.set()
        mock_mcp.HOOKS.clear()


def test_reserved_still_releasable_if_never_dispatched(seeded):
    """reserve_and_commit only — recover release still refunds and clears claim."""
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        _res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="never-dispatch", tool="search", server="mock"
        )
        inv_id = inv.id
    assert cx.remaining(m.id) == 95
    with cx.session() as s:
        inv = s.get(Invocation, inv_id)
        assert inv.status == "reserved"
        out = ledger.recover_reserved(s, inv, mode="release")
        assert out.status == "released"
        assert s.query(IdempotencyRecord).filter_by(idempotency_key="never-dispatch").count() == 0
    assert cx.remaining(m.id) == 100


def test_finalize_success_refuses_reserved_without_executing(seeded):
    """Success requires the executing barrier; reserved must not commit a receipt."""
    from crossing import billing, receipts
    from crossing.models import Receipt

    cx, p, a, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="r8-no-exec", tool="search", server="mock"
        )
        rid, iid = res.id, inv.id
        pid, aid, mid = p.id, a.id, m.id

        def receipt_fn(sess):
            return receipts.issue(
                sess,
                principal_id=pid,
                mandate_id=mid,
                reservation_id=rid,
                tool="search",
                server="mock",
                amount_cents=5,
                result={"ok": True},
                agent_id=aid,
                outcome="ok",
            )

        def billing_fn(sess, rec):
            return billing.enqueue(sess, receipt_id=rec.id, amount_cents=5, principal_id=pid)

        fin = ledger.finalize_success(
            s,
            res,
            inv,
            receipt_fn=receipt_fn,
            billing_fn=billing_fn,
            result_json='{"ok":true}',
        )
        assert fin.won is False
        assert inv.status == "reserved"
        assert res.status == "held"
        assert s.query(Receipt).count() == 0
        claim = s.query(IdempotencyRecord).filter_by(idempotency_key="r8-no-exec").one()
        assert claim.status == "in_progress"
    assert cx.remaining(m.id) == 95

