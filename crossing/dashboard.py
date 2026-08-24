"""Server-rendered HTML tables. Intentionally not pretty."""

from __future__ import annotations

import html
from typing import Any, Iterable

from crossing.models import Agent, Invocation, LedgerEvent, Mandate, Outbox, Principal, Receipt, Reservation


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


def render(session, *, account_id: str | None = None, is_admin: bool = True) -> str:
    pq = session.query(Principal)
    if account_id and not is_admin:
        pq = pq.filter(Principal.account_id == account_id)
    principals = pq.all()
    pids = [p.id for p in principals]
    aq = session.query(Agent)
    mq = session.query(Mandate)
    eq = session.query(LedgerEvent)
    rq = session.query(Receipt)
    hq = session.query(Reservation)
    iq = session.query(Invocation)
    if account_id and not is_admin and pids:
        aq = aq.filter(Agent.principal_id.in_(pids))
        mq = mq.filter(Mandate.principal_id.in_(pids))
        eq = eq.filter(LedgerEvent.principal_id.in_(pids))
        rq = rq.filter(Receipt.principal_id.in_(pids))
        hq = hq.filter(Reservation.principal_id.in_(pids))
        iq = iq.filter(Invocation.principal_id.in_(pids))
    elif account_id and not is_admin:
        aq = aq.filter(Agent.id == None)  # noqa: E711 — empty tenant
        mq = mq.filter(Mandate.id == None)  # noqa: E711
        eq = eq.filter(LedgerEvent.id == None)  # noqa: E711
        rq = rq.filter(Receipt.id == None)  # noqa: E711
        hq = hq.filter(Reservation.id == None)  # noqa: E711
        iq = iq.filter(Invocation.id == None)  # noqa: E711
    agents = aq.all()
    mandates = mq.all()
    events = eq.order_by(LedgerEvent.created_at).all()
    recs = rq.all()
    outbox = session.query(Outbox).all() if is_admin else []
    holds = hq.all()
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
        _table(
            "invocations",
            ["id", "status", "tool", "mandate"],
            [(i.id, i.status, i.tool, i.mandate_id) for i in iq.all()],
        ),
        "</body></html>",
    ]
    return "\n".join(parts)
