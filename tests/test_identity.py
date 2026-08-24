from __future__ import annotations

from crossing.policy import Reason


def test_revoked_parent_blocks_child(seeded):
    cx, p, a, m = seeded
    child = cx.create_agent(p.id, "intern", parent_id=a.id)
    cx.revoke_agent(a.id)
    r = cx.invoke(m.id, "search", {"q": "x"})
    assert r.reason == Reason.AGENT_REVOKED
