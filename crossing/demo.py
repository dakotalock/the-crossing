"""15-step in-process story. Prints and asserts denials."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from crossing.policy import Reason
from crossing.sdk import Crossing


def _step(n: int, title: str) -> None:
    print(f"\n=== {n:02d} {title} ===")


def main() -> int:
    os.environ["CROSSING_ALLOW_DEV"] = "1"
    os.environ.pop("STRIPE_SECRET_KEY", None)
    cx = Crossing.in_process("sqlite:///:memory:")

    _step(1, "Create principal Alice")
    alice = cx.create_principal("Alice")
    print("principal", alice.id, alice.name)

    _step(2, "Create parent agent researcher")
    parent = cx.create_agent(alice.id, "researcher")
    print("agent", parent.id, parent.name)

    _step(3, "Issue signed mandate $1.00, tools=search, max_call $1.00")
    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    mandate = cx.issue_mandate(
        alice.id,
        parent.id,
        spend_limit_cents=100,
        max_call_cents=100,
        tools=["search"],
        servers=["mock"],
        expires_at=exp,
        max_subagent_budget_cents=80,
    )
    print("mandate", mandate.id, "remaining", mandate.remaining_cents, "sig", mandate.signature[:20], "…")
    assert mandate.remaining_cents == 100

    _step(4, "Quote search = $0.05")
    q = cx.quote("search")
    print("quote_cents", q)
    assert q == 5

    _step(5, "Invoke search — succeeds")
    r = cx.invoke(mandate.id, "search", {"q": "agent economics"}, idempotency_key="demo-search-1")
    print("ok", r.ok, "remaining", r.remaining_cents, "amount", r.amount_cents)
    assert r.ok and r.remaining_cents == 95
    assert cx.verify_receipt(r.receipt)

    _step(6, "Child agent + attenuated mandate $0.50 search-only")
    child = cx.create_agent(alice.id, "intern", parent_id=parent.id)
    child_m = cx.attenuate(
        mandate.id,
        child.id,
        spend_limit_cents=50,
        max_call_cents=50,
        tools=["search"],
        servers=["mock"],
        expires_at=exp,
        max_subagent_budget_cents=50,
    )
    print("child mandate", child_m.id, "child remaining", child_m.remaining_cents, "parent remaining", cx.remaining(mandate.id))
    assert child_m.remaining_cents == 50
    assert cx.remaining(mandate.id) == 45

    _step(7, "Child invoke search — succeeds")
    cr = cx.invoke(child_m.id, "search", {"q": "attenuation"}, idempotency_key="demo-child-1")
    print("ok", cr.ok, "child remaining", cr.remaining_cents)
    assert cr.ok and cr.remaining_cents == 45

    _step(8, "Purchase $5 denied by mandate (tool + price)")
    denied = cx.invoke(mandate.id, "purchase", {"sku": "gpu"})
    print("denied", denied.reason, denied.detail)
    assert not denied.ok
    assert denied.reason in (Reason.TOOL_NOT_ALLOWED, Reason.CALL_OVER_MAX)

    _step(9, "Child spend escalation denied")
    try:
        cx.attenuate(mandate.id, child.id, spend_limit_cents=10_000, max_call_cents=50, tools=["search"], servers=["mock"], expires_at=exp)
        raise AssertionError("expected spend escalation")
    except Exception as exc:
        print("denied", exc)
        assert Reason.CHILD_SPEND_ESCALATION in str(exc) or getattr(exc, "reason", "") == Reason.CHILD_SPEND_ESCALATION

    _step(10, "Child expiry later than parent denied")
    later = exp + timedelta(days=7)
    try:
        cx.attenuate(mandate.id, child.id, spend_limit_cents=5, max_call_cents=5, tools=["search"], servers=["mock"], expires_at=later)
        raise AssertionError("expected expiry escalation")
    except Exception as exc:
        print("denied", exc)
        assert Reason.CHILD_EXPIRY_ESCALATION in str(exc) or getattr(exc, "reason", "") == Reason.CHILD_EXPIRY_ESCALATION

    _step(11, "Forged mandate invoke denied")
    forged = cx.issue_mandate(
        alice.id,
        parent.id,
        spend_limit_cents=20,
        max_call_cents=20,
        tools=["search"],
        servers=["mock"],
        expires_at=exp,
        signature="00" * 64,
        pubkey_hex="11" * 32,
        verify=False,
        nonce="forged-nonce-demo",
    )
    fr = cx.invoke(forged.id, "search", {"q": "forge"})
    print("denied", fr.reason)
    assert not fr.ok and fr.reason == Reason.MANDATE_FORGED

    _step(12, "Expired mandate denied")
    past = datetime.now(timezone.utc) - timedelta(seconds=2)
    expired = cx.issue_mandate(
        alice.id,
        parent.id,
        spend_limit_cents=20,
        max_call_cents=20,
        tools=["search"],
        servers=["mock"],
        expires_at=past,
    )
    er = cx.invoke(expired.id, "search", {"q": "late"})
    print("denied", er.reason)
    assert not er.ok and er.reason == Reason.MANDATE_EXPIRED

    _step(13, "Replay invoke nonce denied")
    nonce = "replay-nonce-demo"
    first = cx.invoke(mandate.id, "search", {"q": "n1"}, nonce=nonce, idempotency_key="demo-n1")
    assert first.ok
    replay = cx.invoke(mandate.id, "search", {"q": "n2"}, nonce=nonce, idempotency_key="demo-n2")
    print("denied", replay.reason)
    assert not replay.ok and replay.reason == Reason.NONCE_REPLAY

    _step(14, "Revoked agent denied")
    cx.revoke_agent(child.id)
    rr = cx.invoke(child_m.id, "search", {"q": "ghost"})
    print("denied", rr.reason)
    assert not rr.ok and rr.reason == Reason.AGENT_REVOKED

    _step(15, "Receipt + remaining budget")
    print("parent remaining cents", cx.remaining(mandate.id))
    print("child remaining cents", cx.remaining(child_m.id))
    assert r.receipt and cx.verify_receipt(r.receipt)
    print("receipt valid", True)
    print("\nDEMO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
