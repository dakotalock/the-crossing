from __future__ import annotations

from crossing.policy import Reason


def test_duplicate_invoke(seeded):
    cx, _, _, m = seeded
    a = cx.invoke(m.id, "search", {"q": "same"}, idempotency_key="dup-1")
    b = cx.invoke(m.id, "search", {"q": "same"}, idempotency_key="dup-1")
    assert a.ok and b.ok
    assert b.replayed is True
    assert a.remaining_cents == b.remaining_cents == 95
    assert cx.remaining(m.id) == 95


def test_purchase_denied(seeded):
    cx, _, _, m = seeded
    r = cx.invoke(m.id, "purchase", {"sku": "x"})
    assert r.ok is False
    assert r.reason in (Reason.TOOL_NOT_ALLOWED, Reason.CALL_OVER_MAX)
    assert cx.remaining(m.id) == 100
