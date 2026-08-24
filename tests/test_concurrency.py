from __future__ import annotations

import threading

from crossing import db
from crossing.identity import create_agent, create_principal
from crossing.lifecycle import invoke
from crossing.mandate import issue_mandate


def test_ten_threads_cannot_overspend(cx):
    """$1 remaining, $0.20/call, 10 racers → at most 5 commits, remaining >= 0."""
    from datetime import datetime, timedelta, timezone

    with db.session_scope() as s:
        p = create_principal(s, "Racer")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search", "purchase"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mid, pid = m.id, p.id

    # Monkeypatch price for this test via mock + pricing
    from crossing import pricing

    pricing.PRICES_CENTS[("mock", "search")] = 20
    results: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        session = db.get_session()
        try:
            r = invoke(
                session,
                mandate_id=mid,
                tool="search",
                arguments={"q": str(i)},
                idempotency_key=f"race-{i}",
                nonce=f"race-n-{i}",
            )
            session.commit()
            with lock:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                results.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pricing.PRICES_CENTS[("mock", "search")] = 5

    oks = [r for r in results if getattr(r, "ok", False)]
    dens = [r for r in results if getattr(r, "ok", True) is False]
    assert len(oks) == 5, [(getattr(r, "ok", None), getattr(r, "reason", r)) for r in results]
    assert len(dens) == 5
    assert all(d.reason == "BUDGET_EXCEEDED" for d in dens)
    rem = cx.remaining(mid)
    assert rem == 0
    spent = sum(r.amount_cents for r in oks)
    assert spent == 100
    from crossing.models import IdempotencyRecord

    with cx.session() as s:
        leftover = [c for c in s.query(IdempotencyRecord).all() if c.status == "in_progress"]
        assert leftover == [], [(c.idempotency_key, c.status) for c in leftover]


def test_same_idempotency_key_one_execution(cx):
    """Two threads, same key + request: one mock execute, budget once."""
    from datetime import datetime, timedelta, timezone

    from crossing import mock_mcp
    from crossing.models import IdempotencyRecord, Reservation

    with cx.session() as s:
        p = create_principal(s, "IdemRacer")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mid = m.id

    import time

    calls: list = []
    lock = threading.Lock()

    def slow_search(args, **_ids):
        with lock:
            calls.append(args)
        time.sleep(0.4)
        return {"hits": [{"title": "once"}], "q": args.get("q")}

    mock_mcp.HOOKS["search"] = slow_search
    results: list = []

    def worker() -> None:
        session = db.get_session()
        try:
            r = invoke(
                session,
                mandate_id=mid,
                tool="search",
                arguments={"q": "same"},
                idempotency_key="shared-key",
            )
            session.commit()
            with lock:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                results.append(exc)
        finally:
            session.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        mock_mcp.HOOKS.clear()

    assert len(calls) == 1, calls
    oks = [r for r in results if getattr(r, "ok", False)]
    others = [r for r in results if r not in oks]
    assert len(oks) == 1
    assert cx.remaining(mid) == 95
    with cx.session() as s:
        holds = [h for h in s.query(Reservation).all() if h.status == "committed"]
        assert len(holds) == 1
        claims = s.query(IdempotencyRecord).all()
        assert len(claims) == 1
    # loser is in-progress, replay, or a wait-or-conflict deny — not a second execute
    if others:
        o = others[0]
        if getattr(o, "ok", None) is False:
            assert o.reason in ("IN_PROGRESS", "IDEMPOTENCY_CONFLICT") or o.replayed
        elif getattr(o, "replayed", False):
            pass


def test_max_calls_atomic_under_race(cx):
    """max_calls=1, plenty of remaining: N concurrent invokes → exactly one success."""
    from datetime import datetime, timedelta, timezone

    from crossing.models import LedgerEvent, Mandate
    from crossing.policy import Reason

    n = 8
    with db.session_scope() as s:
        p = create_principal(s, "MaxCallRacer")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=1000,
            max_call_cents=100,
            max_calls=1,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        mid = m.id

    results: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        session = db.get_session()
        try:
            r = invoke(
                session,
                mandate_id=mid,
                tool="search",
                arguments={"q": str(i)},
                idempotency_key=f"max-calls-{i}",
                nonce=f"max-calls-n-{i}",
            )
            session.commit()
            with lock:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                results.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if getattr(r, "ok", False)]
    dens = [r for r in results if getattr(r, "ok", True) is False]
    assert len(oks) == 1, [(getattr(r, "ok", None), getattr(r, "reason", r)) for r in results]
    assert len(dens) == n - 1
    assert all(d.reason == Reason.MAX_CALLS_EXCEEDED for d in dens)
    with cx.session() as s:
        row = s.get(Mandate, mid)
        assert row.calls_used == 1
        assert row.remaining_cents == 1000 - 5
        denies = [e for e in s.query(LedgerEvent).all() if e.kind == "deny"]
        assert denies
        assert all(e.note == Reason.MAX_CALLS_EXCEEDED for e in denies)


def test_concurrent_child_escrow_cannot_over_allocate(cx):
    """Parent remaining 100; two $80 children → one succeeds, parent remaining 20."""
    from datetime import datetime, timedelta, timezone

    from crossing.models import Mandate
    from crossing.policy import PolicyDenied, Reason

    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    with db.session_scope() as s:
        p = create_principal(s, "ChildEscrowRacer")
        a = create_agent(s, p.id, "parent")
        child = create_agent(s, p.id, "kid", parent_id=a.id)
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            max_subagent_budget_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=exp,
        )
        mid, pid, cid = m.id, p.id, child.id

    results: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        session = db.get_session()
        try:
            cm = issue_mandate(
                session,
                principal_id=pid,
                agent_id=cid,
                spend_limit_cents=80,
                max_call_cents=80,
                max_subagent_budget_cents=80,
                tools=["search"],
                servers=["mock"],
                expires_at=exp,
                parent_mandate_id=mid,
            )
            session.commit()
            with lock:
                results.append(cm)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                results.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    kids = [r for r in results if isinstance(r, Mandate)]
    fails = [r for r in results if r not in kids]
    assert len(kids) == 1, results
    assert len(fails) == 1
    assert isinstance(fails[0], PolicyDenied)
    assert fails[0].reason == Reason.CHILD_SPEND_ESCALATION
    assert cx.remaining(mid) == 20
    with cx.session() as s:
        children = [row for row in s.query(Mandate).all() if row.parent_mandate_id == mid]
        assert len(children) == 1
        assert sum(c.spend_limit_cents for c in children) == 80


def test_commit_and_release_cas_one_winner(cx):
    """Concurrent commit and release on one hold: one terminal event, no over-refund."""
    from datetime import datetime, timedelta, timezone

    from crossing import ledger
    from crossing.mandate import load_live_mandate
    from crossing.models import LedgerEvent, Mandate, Reservation

    with db.session_scope() as s:
        p = create_principal(s, "CasRacer")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        res = ledger.reserve(s, load_live_mandate(s, m.id), 5)
        rid, mid, original = res.id, m.id, m.spend_limit_cents

    lock = threading.Lock()
    outcomes: list = []

    def do_commit() -> None:
        session = db.get_session()
        try:
            r = session.get(Reservation, rid)
            out = ledger.commit(session, r)
            session.commit()
            with lock:
                outcomes.append(("commit", out.status))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes.append(("commit", exc))
        finally:
            session.close()

    def do_release() -> None:
        session = db.get_session()
        try:
            r = session.get(Reservation, rid)
            out = ledger.release(session, r)
            session.commit()
            with lock:
                outcomes.append(("release", out.status))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes.append(("release", exc))
        finally:
            session.close()

    threads = [threading.Thread(target=do_commit), threading.Thread(target=do_release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    remaining = cx.remaining(mid)
    assert remaining <= original
    with cx.session() as s:
        hold = s.get(Reservation, rid)
        assert hold.status in ("committed", "released")
        terminals = [e for e in s.query(LedgerEvent).all() if e.kind in ("commit", "release")]
        assert len(terminals) == 1
        row = s.get(Mandate, mid)
        if hold.status == "committed":
            assert remaining == original - 5
            assert row.calls_used == 1
            assert terminals[0].kind == "commit"
        else:
            assert remaining == original
            assert row.calls_used == 0
            assert terminals[0].kind == "release"


def test_finalize_success_vs_recover_release_one_universe(cx):
    """Success finalize and recover-release race: exactly one terminal universe."""
    from datetime import datetime, timedelta, timezone

    from crossing import billing, ledger, receipts
    from crossing.mandate import load_live_mandate
    from crossing.models import IdempotencyRecord, Invocation, Mandate, Outbox, Receipt, Reservation

    with db.session_scope() as s:
        p = create_principal(s, "FinalizeRacer")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        res, inv = ledger.reserve_and_commit(
            s, load_live_mandate(s, m.id), 5, idempotency_key="fin-race", tool="search", server="mock"
        )
        rid, iid, mid, original = res.id, inv.id, m.id, m.spend_limit_cents
        pid = p.id
        aid = a.id

    lock = threading.Lock()
    outcomes: list = []

    def do_success() -> None:
        session = db.get_session()
        try:
            hold = session.get(Reservation, rid)
            invocation = session.get(Invocation, iid)
            marked = ledger.mark_executing(session, invocation)
            if not marked.won:
                with lock:
                    outcomes.append(("success", False))
                return

            def receipt_fn(s):
                return receipts.issue(
                    s,
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

            def billing_fn(s, rec):
                return billing.enqueue(s, receipt_id=rec.id, amount_cents=5, principal_id=pid)

            fin = ledger.finalize_success(
                session,
                hold,
                invocation,
                receipt_fn=receipt_fn,
                billing_fn=billing_fn,
                result_json='{"ok":true}',
            )
            with lock:
                outcomes.append(("success", fin.won))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes.append(("success", exc))
        finally:
            session.close()

    def do_release() -> None:
        session = db.get_session()
        try:
            invocation = session.get(Invocation, iid)
            out = ledger.recover_reserved(session, invocation, mode="release")
            with lock:
                outcomes.append(("release", out.status))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes.append(("release", exc))
        finally:
            session.close()

    threads = [threading.Thread(target=do_success), threading.Thread(target=do_release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    remaining = cx.remaining(mid)
    with cx.session() as s:
        hold = s.get(Reservation, rid)
        invocation = s.get(Invocation, iid)
        claim = s.query(IdempotencyRecord).filter_by(idempotency_key="fin-race").one_or_none()
        receipts_n = s.query(Receipt).count()
        outbox_n = s.query(Outbox).count()
        mandate = s.get(Mandate, mid)
        assert hold.status in ("committed", "released")
        if hold.status == "committed":
            assert invocation.status == "committed"
            assert claim is not None and claim.status == "completed"
            assert receipts_n == 1
            assert outbox_n == 1
            assert remaining == original - 5
            assert mandate.remaining_cents == original - 5
            assert mandate.calls_used == 1
        else:
            assert invocation.status == "released"
            assert claim is None
            assert receipts_n == 0
            assert outbox_n == 0
            assert remaining == original
            assert mandate.remaining_cents == original
            assert mandate.calls_used == 0
        # Never both billed and refunded; never committed hold + cleared claim.
        billed = outbox_n > 0
        refunded = remaining == original
        assert not (billed and refunded)
        assert not (hold.status == "committed" and claim is None)
        assert not (receipts_n > 0 and refunded)

def test_recover_ambiguous_cannot_overwrite_committed(cx):
    """Stale executing object must not CAS/ORM-write committed → ambiguous."""
    from datetime import datetime, timedelta, timezone

    from crossing import billing, ledger, receipts
    from crossing.mandate import load_live_mandate
    from crossing.models import IdempotencyRecord, Invocation, Mandate, Receipt, Reservation

    with db.session_scope() as s:
        p = create_principal(s, "StaleAmb")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        res, inv = ledger.reserve_and_commit(
            s, load_live_mandate(s, m.id), 5, idempotency_key="r8-stale", tool="search", server="mock"
        )
        rid, iid, mid = res.id, inv.id, m.id
        pid, aid = p.id, a.id
        original = m.spend_limit_cents

    s_stale = db.get_session()
    s_ok = db.get_session()
    try:
        marked = ledger.mark_executing(s_ok, s_ok.get(Invocation, iid))
        assert marked.won
        stale = s_stale.get(Invocation, iid)
        assert stale.status == "executing"
        # Release SQLite BEGIN IMMEDIATE so the other session can finalize.
        # expire_on_commit=False keeps this identity-map row stale as executing.
        s_stale.commit()

        hold = s_ok.get(Reservation, rid)
        invocation = s_ok.get(Invocation, iid)

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
            s_ok,
            hold,
            invocation,
            receipt_fn=receipt_fn,
            billing_fn=billing_fn,
            result_json='{"ok":true}',
        )
        assert fin.won
        assert stale.status == "executing"  # identity map still stale
        out = ledger.recover_reserved(s_stale, stale, mode="ambiguous")
        assert out.status == "committed"
    finally:
        s_stale.close()
        s_ok.close()

    remaining = cx.remaining(mid)
    with cx.session() as s:
        inv = s.get(Invocation, iid)
        hold = s.get(Reservation, rid)
        claim = s.query(IdempotencyRecord).filter_by(idempotency_key="r8-stale").one()
        assert inv.status == "committed"
        assert inv.status != "ambiguous"
        assert hold.status == "committed"
        assert s.query(Receipt).count() == 1
        assert remaining == original - 5
        assert s.get(Mandate, mid).remaining_cents == original - 5
        assert claim.status == "completed"


def test_recover_ambiguous_vs_mark_executing_one_winner(cx):
    """reserved→ambiguous vs reserved→executing: exactly one reserved-row winner."""
    from datetime import datetime, timedelta, timezone

    from crossing import ledger
    from crossing.mandate import load_live_mandate
    from crossing.models import Invocation, Reservation

    with db.session_scope() as s:
        p = create_principal(s, "AmbMarkRace")
        a = create_agent(s, p.id, "bot")
        m = issue_mandate(
            s,
            principal_id=p.id,
            agent_id=a.id,
            spend_limit_cents=100,
            max_call_cents=100,
            tools=["search"],
            servers=["mock"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        res, inv = ledger.reserve_and_commit(
            s, load_live_mandate(s, m.id), 5, idempotency_key="r8-race", tool="search", server="mock"
        )
        rid, iid = res.id, inv.id

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    outcomes: dict = {}

    def do_recover() -> None:
        session = db.get_session()
        try:
            barrier.wait(timeout=10)
            invocation = session.get(Invocation, iid)
            out = ledger.recover_reserved(session, invocation, mode="ambiguous")
            with lock:
                outcomes["recover"] = out.status
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes["recover"] = exc
        finally:
            session.close()

    def do_mark() -> None:
        session = db.get_session()
        try:
            barrier.wait(timeout=10)
            invocation = session.get(Invocation, iid)
            marked = ledger.mark_executing(session, invocation)
            with lock:
                outcomes["mark_won"] = marked.won
                outcomes["mark_status"] = marked.invocation.status
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            with lock:
                outcomes["mark_won"] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=do_recover), threading.Thread(target=do_mark)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with cx.session() as s:
        inv = s.get(Invocation, iid)
        hold = s.get(Reservation, rid)
        assert inv.status != "reserved"
        assert inv.status in ("executing", "ambiguous")
        assert inv.status != "committed"
        mark_won = outcomes["mark_won"] is True
        if inv.status == "executing":
            assert mark_won
        if not mark_won:
            assert inv.status == "ambiguous"
            assert outcomes["recover"] == "ambiguous"
        assert hold.status == "held"
