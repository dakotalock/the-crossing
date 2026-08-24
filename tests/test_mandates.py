from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crossing.policy import PolicyDenied, Reason


def test_forged_mandate(seeded):
    cx, p, a, _ = seeded
    forged = cx.issue_mandate(
        p.id,
        a.id,
        spend_limit_cents=20,
        tools=["search"],
        servers=["mock"],
        signature="aa" * 64,
        pubkey_hex="bb" * 32,
        verify=False,
        nonce="forged-1",
    )
    r = cx.invoke(forged.id, "search", {"q": "x"})
    assert r.ok is False
    assert r.reason == Reason.MANDATE_FORGED


def test_expired_mandate(seeded):
    cx, p, a, _ = seeded
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    m = cx.issue_mandate(p.id, a.id, 20, tools=["search"], servers=["mock"], expires_at=past)
    r = cx.invoke(m.id, "search", {"q": "x"})
    assert r.reason == Reason.MANDATE_EXPIRED


def test_replay_nonce(seeded):
    cx, _, _, m = seeded
    a = cx.invoke(m.id, "search", {"q": "1"}, nonce="n-once", idempotency_key="a")
    assert a.ok
    b = cx.invoke(m.id, "search", {"q": "2"}, nonce="n-once", idempotency_key="b")
    assert b.reason == Reason.NONCE_REPLAY


def test_child_spend_escalation(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(m.id, child.id, spend_limit_cents=10_000, tools=["search"], servers=["mock"])
    assert ei.value.reason == Reason.CHILD_SPEND_ESCALATION


def test_child_tools_escalation(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(m.id, child.id, 10, tools=["search", "purchase"], servers=["mock"])
    assert ei.value.reason == Reason.CHILD_TOOLS_ESCALATION


def test_child_expiry_escalation(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    later = datetime.now(timezone.utc) + timedelta(days=30)
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(m.id, child.id, 10, tools=["search"], servers=["mock"], expires_at=later)
    assert ei.value.reason == Reason.CHILD_EXPIRY_ESCALATION


def test_budget_escalation_max_subagent(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    with pytest.raises(PolicyDenied) as ei:
        cx.attenuate(
            m.id,
            child.id,
            spend_limit_cents=10,
            tools=["search"],
            servers=["mock"],
            expires_at=exp,
            max_subagent_budget_cents=10_000,
        )
    assert ei.value.reason == Reason.CHILD_BUDGET_ESCALATION


def test_child_happy_path_escrow(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "kid", parent_id=a.id)
    exp = datetime.now(timezone.utc) + timedelta(minutes=30)
    cm = cx.attenuate(m.id, child.id, 40, tools=["search"], servers=["mock"], expires_at=exp, max_subagent_budget_cents=40)
    assert cm.remaining_cents == 40
    assert cx.remaining(m.id) == 60
