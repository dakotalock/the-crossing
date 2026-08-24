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

    def slow_search(args):
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
