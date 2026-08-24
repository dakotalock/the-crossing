"""In-process paid MCP tools: search $0.05, purchase/expensive $5."""

from __future__ import annotations

from typing import Any, Callable

TOOLS = {
    "search": {
        "name": "search",
        "description": "Paid search. $0.05 per call.",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    "purchase": {
        "name": "purchase",
        "description": "Expensive purchase. $5.00 per call.",
        "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}},
    },
    "expensive": {
        "name": "expensive",
        "description": "Alias of purchase. $5.00 per call.",
        "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}},
    },
}

# Tests may replace these callables.
HOOKS: dict[str, Callable[[dict[str, Any]], Any]] = {}


class MCPError(RuntimeError):
    pass


def list_tools() -> list[dict[str, Any]]:
    return list(TOOLS.values())


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    arguments = arguments or {}
    if name in HOOKS:
        return HOOKS[name](arguments)
    if name == "search":
        q = arguments.get("q") or arguments.get("query") or ""
        return {"hits": [{"title": f"result for {q}", "url": "https://example.invalid"}], "q": q}
    if name in ("purchase", "expensive"):
        sku = arguments.get("sku") or "sku"
        return {"order_id": "ord_mock", "sku": sku, "charged": 5.0}
    raise MCPError(f"unknown tool {name}")
