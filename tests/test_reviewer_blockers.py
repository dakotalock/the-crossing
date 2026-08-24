from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from crossing import auth, billing, db, ledger, metrics
from crossing.api import app
from crossing.mandate import load_live_mandate
from crossing.models import Account, ApiKey, IdempotencyRecord, Invocation, Outbox, Principal, Receipt, Reservation
from crossing.policy import PolicyDenied, Reason


def _attach(cx, principal, cus="cus_x", status=None):
    with cx.session() as s:
        acct = s.get(Account, s.get(Principal, principal.id).account_id)
        acct.stripe_customer_id = cus
        if status is not None:
            acct.stripe_status = status


def test_invoke_billing_required_without_customer(seeded, monkeypatch):
    cx, _, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    r = cx.invoke(m.id, "search", {"q": "pay"}, idempotency_key="bill-req-1")
    assert r.ok is False
    assert r.reason == Reason.BILLING_REQUIRED
    assert cx.remaining(m.id) == 100
    with cx.session() as s:
        assert s.query(Outbox).count() == 0


def test_invoke_billing_required_canceled_status(seeded, monkeypatch):
    cx, p, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    _attach(cx, p, "cus_canceled", status="canceled")
    r = cx.invoke(m.id, "search", {"q": "pay"}, idempotency_key="bill-req-2")
    assert r.ok is False
    assert r.reason == Reason.BILLING_REQUIRED
    assert cx.remaining(m.id) == 100


def test_invoke_does_not_call_post_stripe(seeded, monkeypatch):
    cx, p, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    _attach(cx, p)
    calls = {"n": 0}

    def spy(_payload):
        calls["n"] += 1
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setattr(billing, "post_stripe", spy)
    r = cx.invoke(m.id, "search", {"q": "nohttp"}, idempotency_key="nohttp-1")
    assert r.ok
    assert calls["n"] == 0
    with cx.session() as s:
        assert s.query(Outbox).one().status == "pending"


def test_missing_account_customer_does_not_use_global_env(seeded, monkeypatch):
    cx, p, _, _ = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_CUSTOMER_ID", "cus_global_should_not_meter")
    with cx.session() as s:
        row = billing.enqueue(s, receipt_id="no-global", amount_cents=5, principal_id=p.id)
        payload = json.loads(row.payload_json)
        assert payload.get("customer_id") is None
        assert "cus_global_should_not_meter" not in row.payload_json


def test_metrics_see_dead_outbox(seeded):
    cx, p, _, _ = seeded
    with cx.session() as s:
        billing.enqueue(s, receipt_id="dead-vis", amount_cents=5, principal_id=p.id)
        row = s.query(Outbox).one()
        row.status = "dead"
        row.attempts = billing.MAX_ATTEMPTS
        rid = row.id
    snap = metrics.snapshot()
    assert snap["outbox_dead"] >= 1
    text = metrics.prometheus_text()
    assert "crossing_outbox_dead" in text
    client = TestClient(app)
    mx = client.get("/metrics")
    assert "crossing_outbox_dead" in mx.text
    with cx.session() as s:
        assert s.get(Outbox, rid).status == "dead"


def test_admin_requeue_dead(cx):
    from crossing.identity import create_principal

    client = TestClient(app)
    with cx.session() as s:
        p = create_principal(s, "Requeue")
        admin = auth.issue_api_key(s, account_id=p.account_id, kind="admin")
        row = billing.enqueue(s, receipt_id="dead-rq", amount_cents=5, principal_id=p.id)
        row.status = "dead"
        row.attempts = billing.MAX_ATTEMPTS
        oid, secret = row.id, admin.secret
    r = client.post(f"/v1/admin/outbox/{oid}/requeue", headers={"X-API-Key": secret})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    with cx.session() as s:
        row = s.get(Outbox, oid)
        assert row.status == "pending"
        assert row.attempts == 0


def test_requeue_dead_resets_attempts_drain_posts_stripe(seeded, monkeypatch):
    """Dead rows at MAX_ATTEMPTS must reset attempts so claim/drain can post_stripe."""
    from crossing import worker

    cx, p, _, _ = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    calls = {"n": 0}

    def spy(_payload):
        calls["n"] += 1
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setattr(billing, "post_stripe", spy)
    with cx.session() as s:
        row = billing.enqueue(s, receipt_id="dead-drain", amount_cents=5, principal_id=p.id)
        row.status = "dead"
        row.attempts = billing.MAX_ATTEMPTS
        oid = row.id
    billing.drain_outbox()
    assert calls["n"] == 0
    with cx.session() as s:
        billing.requeue_dead(s, oid)
        row = s.get(Outbox, oid)
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.attempts < billing.MAX_ATTEMPTS
    n = worker.run_once()
    assert n >= 1
    assert calls["n"] == 1
    with cx.session() as s:
        row = s.get(Outbox, oid)
        assert row.status == "sent"


def test_post_stripe_payload_does_not_claim_unsent_fees(seeded, monkeypatch):
    cx, p, _, m = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("CROSSING_FEE_BPS", "4")
    _attach(cx, p)
    seen = []

    def spy(payload):
        seen.append(payload)
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setattr(billing, "post_stripe", spy)
    r = cx.invoke(m.id, "search", {"q": "fee"}, idempotency_key="fee-pay")
    assert r.ok
    billing.drain_outbox()
    assert seen
    assert int(seen[0].get("platform_fee_invoice_cents") or 0) == 0
    assert "fee" not in str(seen[0].get("amount_cents"))
    with cx.session() as s:
        acct = s.get(Account, s.get(Principal, p.id).account_id)
        assert acct.fee_invoiced_cents == 0
        assert acct.fee_microcents > 0


def test_two_accounts_cannot_share_stripe_customer(cx):
    p1 = cx.create_principal("T1")
    p2 = cx.create_principal("T2")
    with cx.session() as s:
        billing.attach_stripe_customer(s, p1.account_id, "cus_unique")
        try:
            billing.attach_stripe_customer(s, p2.account_id, "cus_unique")
            raise AssertionError("expected unique customer")
        except PolicyDenied:
            pass


def test_idempotency_result_json_omits_tool_payload(seeded, monkeypatch):
    monkeypatch.delenv("CROSSING_RETAIN_PAYLOADS", raising=False)
    cx, _, _, m = seeded
    marker = "unique-tool-hits-xyz"
    r = cx.invoke(m.id, "search", {"q": marker}, idempotency_key="min-1")
    assert r.ok
    assert marker in json.dumps(r.result)
    with cx.session() as s:
        rec = s.query(IdempotencyRecord).filter_by(idempotency_key="min-1").one()
        assert rec.result_json
        assert marker not in rec.result_json
        assert '"result": null' in rec.result_json or "hits" not in rec.result_json
    replay = cx.invoke(m.id, "search", {"q": marker}, idempotency_key="min-1")
    assert replay.ok and replay.replayed


def test_read_scope_cannot_create_principal_or_agent(cx):
    from crossing.identity import create_principal

    client = TestClient(app)
    with cx.session() as s:
        p = create_principal(s, "RO")
        issued = auth.issue_api_key(s, account_id=p.account_id, kind="customer", scopes=["read"])
        secret, pid = issued.secret, p.id
    denied_p = client.post("/v1/principals", json={"name": "Nope"}, headers={"X-API-Key": secret})
    assert denied_p.status_code == 403
    denied_a = client.post(
        "/v1/agents", json={"principal_id": pid, "name": "bot"}, headers={"X-API-Key": secret}
    )
    assert denied_a.status_code == 403


def test_customer_cannot_rotate_admin_key(cx):
    from crossing.identity import create_principal

    client = TestClient(app)
    with cx.session() as s:
        p = create_principal(s, "Keys")
        admin = auth.issue_api_key(s, account_id=p.account_id, kind="admin")
        cust = auth.issue_api_key(s, account_id=p.account_id, kind="customer")
        admin_id, admin_secret, cust_secret = admin.record.id, admin.secret, cust.secret
    steal = client.post(f"/v1/keys/{admin_id}/rotate", headers={"X-API-Key": cust_secret})
    assert steal.status_code == 403
    self_ok = client.post(f"/v1/keys/{admin_id}/rotate", headers={"X-API-Key": admin_secret})
    assert self_ok.status_code == 200


def test_read_sibling_cannot_rotate_other_key(cx):
    from crossing.identity import create_principal

    client = TestClient(app)
    with cx.session() as s:
        p = create_principal(s, "SibKeys")
        owner = auth.issue_api_key(s, account_id=p.account_id, kind="customer")
        reader = auth.issue_api_key(
            s, account_id=p.account_id, kind="customer", scopes=["read"]
        )
        owner_id, reader_secret = owner.record.id, reader.secret
    steal = client.post(f"/v1/keys/{owner_id}/rotate", headers={"X-API-Key": reader_secret})
    assert steal.status_code == 403
    steal_rev = client.post(f"/v1/keys/{owner_id}/revoke", headers={"X-API-Key": reader_secret})
    assert steal_rev.status_code == 403
    with cx.session() as s:
        row = s.get(ApiKey, owner_id)
        assert row is not None
        assert row.revoked_at is None


def test_claim_commit_before_http_rollback_does_not_double_post(seeded, monkeypatch):
    cx, p, _, _ = seeded
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    calls: list[str] = []

    def track(payload):
        calls.append(payload.get("receipt_id"))
        return {"ok": True, "stripe_reported": True}

    monkeypatch.setattr(billing, "post_stripe", track)
    with cx.session() as s:
        billing.enqueue(s, receipt_id="claim-http", amount_cents=5, principal_id=p.id)
    s = db.get_session()
    try:
        claimed = billing._claim_outbox_rows(s, limit=1)
        assert claimed
        s.commit()
        billing.post_stripe(json.loads(claimed[0].payload_json))
        s.rollback()
    finally:
        s.close()
    billing.drain_outbox()
    assert calls == ["claim-http"]
    with cx.session() as s:
        assert s.query(Outbox).one().status == "sending"


def test_reconcile_commit_requires_did_execute(seeded):
    cx, p, a, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="rc-ev", tool="search", server="mock"
        )
        assert ledger.mark_executing(s, inv).won
        ledger.mark_executed_fail(s, inv)
        iid, rid = inv.id, res.id
    with cx.session() as s:
        inv = s.get(Invocation, iid)

        def rf(_sess):
            raise AssertionError("must not issue receipt")

        def bf(_sess, _rec):
            raise AssertionError("must not enqueue")

        with pytest.raises(PolicyDenied):
            ledger.reconcile_commit(
                s,
                inv,
                actor="op",
                evidence_ref="ticket",
                evidence_kind=ledger.EVIDENCE_DID_NOT_EXECUTE,
                receipt_fn=rf,
                billing_fn=bf,
            )
        s.refresh(inv)
        assert inv.status == "executed_fail"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 0
        assert s.query(Outbox).count() == 0
    assert cx.remaining(m.id) == 95


def test_expired_mandate_cannot_reserve_even_if_check_fresh_skipped(seeded):
    cx, _, _, m = seeded
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    with cx.session() as s:
        row = s.get(type(m), m.id)
        row.expires_at = past
        s.flush()
        with pytest.raises(PolicyDenied) as ei:
            ledger.reserve(s, row, 5, record_deny=False)
        assert ei.value.reason == Reason.MANDATE_EXPIRED
    assert cx.remaining(m.id) == 100


def test_finalize_release_default_does_not_refund_executed_fail(seeded):
    cx, _, _, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="rel-fail", tool="search", server="mock"
        )
        assert ledger.mark_executing(s, inv).won
        ledger.mark_executed_fail(s, inv)
        fin = ledger.finalize_release(s, inv, res)
        assert fin.won is False
        s.refresh(inv)
        s.refresh(res)
        assert inv.status == "executed_fail"
        assert res.status == "held"
    assert cx.remaining(m.id) == 95


def test_receipt_reservation_id_unique(seeded):
    cx, p, _, m = seeded
    r = cx.invoke(m.id, "search", {"q": "u"}, idempotency_key="uq-rec")
    assert r.ok
    with cx.session() as s:
        rec = s.get(Receipt, r.receipt["id"])
        dup = Receipt(
            id="dup-rec",
            principal_id=p.id,
            mandate_id=m.id,
            reservation_id=rec.reservation_id,
            tool="search",
            server="mock",
            amount_cents=5,
            body_json="{}",
            signature="x",
            pubkey_hex="y",
        )
        s.add(dup)
        try:
            s.flush()
            raise AssertionError("expected unique reservation_id")
        except IntegrityError:
            s.rollback()


def test_compose_postgres_not_public(cx):
    text = Path("/workspace/the-crossing/docker-compose.yml").read_text()
    assert "127.0.0.1:5432:5432" in text
    assert 'POSTGRES_PASSWORD: crossing\n' not in text
    assert "0.0.0.0:5432" not in text
    assert "POSTGRES_PASSWORD" in text
    assert 'command: ["python", "-m", "crossing.worker"]' in text
    caddy = Path("/workspace/the-crossing/Caddyfile").read_text()
    assert "TLS" in caddy
