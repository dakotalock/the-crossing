from __future__ import annotations

from crossing import mock_mcp
from crossing.models import LedgerEvent, Reservation


def test_mcp_error_releases_reservation(seeded):
    cx, _, _, m = seeded

    def boom(_args, **_ids):
        raise mock_mcp.MCPError("upstream failed")

    mock_mcp.HOOKS["search"] = boom
    try:
        r = cx.invoke(m.id, "search", {"q": "boom"}, idempotency_key="mcp-err")
        assert r.ok is False
        assert r.reason == "MCP_ERROR"
        assert cx.remaining(m.id) == 100
        with cx.session() as s:
            holds = s.query(Reservation).all()
            assert holds and holds[0].status == "released"
            kinds = [e.kind for e in s.query(LedgerEvent).all()]
            assert "reserve" in kinds and "release" in kinds
            assert "commit" not in kinds
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
