"""Server-rendered HTML tables. Intentionally not pretty."""

from __future__ import annotations

import html
from typing import Any, Iterable

from crossing.models import Agent, LedgerEvent, Mandate, Outbox, Principal, Receipt, Reservation


def _td(v: Any) -> str:
    if v is None:
        return "<td></td>"
    return f"<td>{html.escape(str(v))}</td>"


def _table(title: str, headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(_td(c) for c in row) + "</tr>")
    return f"<h2>{html.escape(title)}</h2><table border='1' cellpadding='4'><tr>{head}</tr>{''.join(body)}</table>"


def render(session) -> str:
    principals = session.query(Principal).all()
    agents = session.query(Agent).all()
    mandates = session.query(Mandate).all()
    events = session.query(LedgerEvent).order_by(LedgerEvent.created_at).all()
    recs = session.query(Receipt).all()
    outbox = session.query(Outbox).all()
    holds = session.query(Reservation).all()
    parts = [
        "<!doctype html><html><head><title>The Crossing</title></head><body>",
        "<h1>The Crossing</h1><p>Agent Economic Runtime dashboard</p>",
        _table("principals", ["id", "name", "created"], [(p.id, p.name, p.created_at) for p in principals]),
        _table(
            "agents",
            ["id", "principal", "parent", "name", "revoked"],
            [(a.id, a.principal_id, a.parent_id, a.name, a.revoked) for a in agents],
        ),
        _table(
            "mandates",
            ["id", "agent", "spend", "remaining", "max_call", "tools", "expires", "revoked"],
            [
                (
                    m.id,
                    m.agent_id,
                    m.spend_limit_cents,
                    m.remaining_cents,
                    m.max_call_cents,
                    m.tools_json,
                    m.expires_at,
                    m.revoked,
                )
                for m in mandates
            ],
        ),
        _table(
            "ledger_events",
            ["id", "kind", "amount", "remaining_after", "mandate", "note"],
            [(e.id, e.kind, e.amount_cents, e.remaining_after, e.mandate_id, e.note) for e in events],
        ),
        _table(
            "reservations",
            ["id", "status", "amount", "mandate"],
            [(r.id, r.status, r.amount_cents, r.mandate_id) for r in holds],
        ),
        _table(
            "receipts",
            ["id", "tool", "amount", "sig"],
            [(r.id, r.tool, r.amount_cents, r.signature[:16] + "…") for r in recs],
        ),
        _table(
            "outbox",
            ["id", "status", "attempts", "error"],
            [(o.id, o.status, o.attempts, o.last_error) for o in outbox],
        ),
        "</body></html>",
    ]
    return "\n".join(parts)
