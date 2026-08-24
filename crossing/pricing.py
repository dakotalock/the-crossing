"""Tool prices in integer cents."""

from __future__ import annotations

PRICES_CENTS: dict[tuple[str, str], int] = {
    ("mock", "search"): 5,  # $0.05
    ("mock", "purchase"): 500,  # $5.00
    ("mock", "expensive"): 500,
}

DEFAULT_SERVER = "mock"


def quote(tool: str, server: str = DEFAULT_SERVER) -> int:
    key = (server, tool)
    if key not in PRICES_CENTS:
        raise KeyError(f"no price for {server}/{tool}")
    return PRICES_CENTS[key]


def cents_to_dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"
