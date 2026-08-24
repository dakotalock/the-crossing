from __future__ import annotations

from crossing import mock_mcp
from crossing.models import LedgerEvent, Reservation


def test_mcp_error_after_dispatch_does_not_refund(seeded):
    """Tool throw after executing is committed is not an automatic refund."""
    from crossing.models import IdempotencyRecord, Invocation

    cx, _, _, m = seeded

    def boom(_args, **_ids):
        raise mock_mcp.MCPError("upstream failed")

    mock_mcp.HOOKS["search"] = boom
    try:
        r = cx.invoke(m.id, "search", {"q": "boom"}, idempotency_key="mcp-err")
        assert r.ok is False
        assert r.reason == "MCP_ERROR"
        assert cx.remaining(m.id) == 95
        with cx.session() as s:
            holds = s.query(Reservation).all()
            assert holds and holds[0].status == "held"
            invs = s.query(Invocation).all()
            assert invs and invs[0].status == "executed_fail"
            kinds = [e.kind for e in s.query(LedgerEvent).all()]
            assert "reserve" in kinds
            assert "release" not in kinds
            assert "commit" not in kinds
            claim = s.query(IdempotencyRecord).filter_by(idempotency_key="mcp-err").one()
            assert claim.status == "in_progress"
    finally:
        mock_mcp.HOOKS.clear()


def test_proxy_jsonrpc_deny_purchase(seeded):
    from crossing.proxy import handle_jsonrpc

    cx, _, _, m = seeded
    with cx.session() as s:
        out = handle_jsonrpc(
            s,
            m.id,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "purchase", "arguments": {}}},
        )
    assert "error" in out
    assert out["error"]["data"]["reason"] in ("TOOL_NOT_ALLOWED", "CALL_OVER_MAX")
