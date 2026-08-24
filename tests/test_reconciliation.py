from __future__ import annotations

from crossing import billing, db, ledger, receipts
from crossing.mandate import load_live_mandate
from crossing.models import IdempotencyRecord, Invocation, LedgerEvent, Receipt, ReconciliationEvent, Reservation
from crossing.policy import PolicyDenied


def _prep_executing(cx, key: str):
    cx_obj, p, a, m = cx
    with cx_obj.session() as s:
        mandate = load_live_mandate(s, m.id)
        res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key=key, tool="search", server="mock"
        )
        marked = ledger.mark_executing(s, inv)
        assert marked.won
        return res.id, inv.id, p.id, a.id, m.id


def _receipt_billing(pid, aid, mid, rid, inv):
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

    return receipt_fn, billing_fn


def test_cannot_reconcil_release_a_committed(seeded):
    cx, p, a, m = seeded
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "rel-after-commit")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        fin = ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}')
        assert fin.won
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        out = ledger.reconcile_release(
            s,
            inv,
            actor="test",
            evidence_ref="ticket-1",
            evidence_kind=ledger.EVIDENCE_DID_NOT_EXECUTE,
        )
        assert out.won is False
        assert inv.status == "committed"
        assert s.get(Reservation, rid).status == "committed"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 1
    assert cx.remaining(m.id) == 95


def test_cannot_reconcil_commit_a_released(seeded):
    cx, p, a, m = seeded
    with cx.session() as s:
        mandate = load_live_mandate(s, m.id)
        res, inv = ledger.reserve_and_commit(
            s, mandate, 5, idempotency_key="commit-after-rel", tool="search", server="mock"
        )
        ledger.recover_reserved(s, inv, mode="release")
        iid, rid = inv.id, res.id
    assert cx.remaining(m.id) == 100
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        rf, bf = _receipt_billing(p.id, a.id, m.id, rid, inv)
        out = ledger.reconcile_commit(
            s,
            inv,
            actor="test",
            evidence_ref="ticket-2",
            evidence_kind=ledger.EVIDENCE_DID_EXECUTE,
            receipt_fn=rf,
            billing_fn=bf,
        )
        assert out.won is False
        assert inv.status == "released"
        assert s.get(Reservation, rid).status == "released"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 0
    assert cx.remaining(m.id) == 100


def test_did_execute_cannot_use_released_path(seeded):
    cx, p, a, m = seeded
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "did-exec")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        ledger.mark_executed_fail(s, inv)
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        try:
            ledger.reconcile_release(
                s,
                inv,
                actor="op",
                evidence_ref="prov-1",
                evidence_kind=ledger.EVIDENCE_DID_EXECUTE,
            )
            raise AssertionError("expected refuse")
        except PolicyDenied:
            pass
        s.refresh(inv)
        assert inv.status == "executed_fail"
        assert s.get(Reservation, rid).status == "held"


def test_reconcil_commit_does_not_double_bill(seeded):
    cx, p, a, m = seeded
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "double-bill")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        assert ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}').won
    # force a would-be reconcil from a billed row: CAS must no-op
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        out = ledger.reconcile_commit(
            s, inv, actor="op", evidence_ref="e", evidence_kind=ledger.EVIDENCE_DID_EXECUTE, receipt_fn=rf, billing_fn=bf
        )
        assert out.won is False
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 1
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="commit").count() == 1


def test_race_reconcil_commit_vs_late_finalize_success(seeded):
    """Both orderings: exactly one billed universe per attempt."""
    from crossing.models import Outbox

    cx, p, a, m = seeded

    # Order 1: finalize_success wins; reconcil_commit is a no-op.
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "race-c1")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        assert ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}').won
        s.refresh(inv)
        late = ledger.reconcile_commit(
            s, inv, actor="op", evidence_ref="late", evidence_kind=ledger.EVIDENCE_DID_EXECUTE, receipt_fn=rf, billing_fn=bf
        )
        assert late.won is False
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 1
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="commit").count() == 1
        assert inv.status == "committed"

    # Order 2: executed_fail + reconcil_commit wins; late finalize_success is a no-op.
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "race-c2")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        ledger.mark_executed_fail(s, inv)
        s.refresh(inv)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        won = ledger.reconcile_commit(
            s, inv, actor="op", evidence_ref="first", evidence_kind=ledger.EVIDENCE_DID_EXECUTE, receipt_fn=rf, billing_fn=bf
        )
        assert won.won
        s.refresh(inv)
        s.refresh(res)
        late = ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}')
        assert late.won is False
        assert inv.status == "reconciled_committed"
        assert res.status == "committed"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 1
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="commit").count() == 1
        assert s.query(Invocation).filter_by(id=iid).count() == 1
    assert cx.remaining(m.id) == 90


def test_race_reconcil_release_vs_finalize_success(seeded):
    """Both orderings: one universe — billed XOR refunded — never both."""
    cx, p, a, m = seeded

    # Order 1: finalize_success commits; reconcil_release cannot unwind it.
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "race-r1")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        assert ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}').won
        s.refresh(inv)
        lost = ledger.reconcile_release(
            s, inv, actor="op", evidence_ref="nope", evidence_kind=ledger.EVIDENCE_DID_NOT_EXECUTE
        )
        assert lost.won is False
        assert s.get(Reservation, rid).status == "committed"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 1
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="release").count() == 0
        claim = s.query(IdempotencyRecord).filter_by(idempotency_key="race-r1").one()
        assert claim.status == "completed"
    assert cx.remaining(m.id) == 95

    # Order 2: reconcil_release wins; late finalize_success cannot bill.
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "race-r2")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        res = s.get(Reservation, rid)
        ledger.mark_executed_fail(s, inv)
        s.refresh(inv)
        won = ledger.reconcile_release(
            s, inv, actor="op", evidence_ref="none", evidence_kind=ledger.EVIDENCE_DID_NOT_EXECUTE
        )
        assert won.won
        s.refresh(inv)
        s.refresh(res)
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        late = ledger.finalize_success(s, res, inv, receipt_fn=rf, billing_fn=bf, result_json='{"ok":true}')
        assert late.won is False
        assert res.status == "released"
        assert inv.status == "reconciled_released"
        assert s.query(Receipt).filter_by(reservation_id=rid).count() == 0
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="commit").count() == 0
        assert s.query(LedgerEvent).filter_by(reservation_id=rid, kind="release").count() == 1
        assert s.query(IdempotencyRecord).filter_by(idempotency_key="race-r2").count() == 0
        assert s.query(Invocation).filter_by(id=iid).count() == 1
    assert cx.remaining(m.id) == 95  # first attempt billed, second refunded


def test_evidence_ref_required_and_event_appended(seeded):
    cx, p, a, m = seeded
    rid, iid, pid, aid, mid = _prep_executing((cx, p, a, m), "ev-ref")
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        ledger.mark_executed_fail(s, inv)
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        try:
            ledger.reconcile_commit(
                s, inv, actor="op", evidence_ref="  ", evidence_kind=ledger.EVIDENCE_DID_EXECUTE
            )
            raise AssertionError("empty evidence")
        except PolicyDenied:
            pass
        rf, bf = _receipt_billing(pid, aid, mid, rid, inv)
        out = ledger.reconcile_commit(
            s,
            inv,
            actor="api-key-xyz",
            evidence_ref="vendor-log-9",
            evidence_kind=ledger.EVIDENCE_DID_EXECUTE,
            receipt_fn=rf,
            billing_fn=bf,
        )
        assert out.won
        ev = s.query(ReconciliationEvent).filter_by(invocation_id=iid).one()
        assert ev.actor == "api-key-xyz"
        assert ev.evidence_ref == "vendor-log-9"
        assert ev.to_status == "reconciled_committed"
        assert s.query(ReconciliationEvent).count() == 1
    assert cx.remaining(m.id) == 95
