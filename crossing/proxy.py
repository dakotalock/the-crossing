"""MCP JSON-RPC tools/call interceptor (stdio or in-process)."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from crossing.lifecycle import InvokeResult, invoke
from crossing.policy import PolicyDenied


def handle_jsonrpc(session, mandate_id: str, message: dict[str, Any]) -> dict[str, Any]:
    mid = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "tools/list":
        from crossing.mock_mcp import list_tools

        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": list_tools()}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    idem = params.get("idempotency_key") or (message.get("params") or {}).get("idempotencyKey")
    try:
        result = invoke(
            session,
            mandate_id=mandate_id,
            tool=name,
            arguments=arguments,
            idempotency_key=idem,
            nonce=params.get("nonce"),
        )
    except PolicyDenied as exc:
        result = InvokeResult(ok=False, reason=exc.reason, detail=str(exc.detail))
    if result.ok:
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(result.to_dict())}]}}
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32000, "message": result.reason or "denied", "data": result.to_dict()},
    }


def stdio_loop(session, mandate_id: str, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        out = handle_jsonrpc(session, mandate_id, msg)
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
