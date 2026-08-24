"""Public landing plus operator dashboard."""

from __future__ import annotations

import html
from typing import Any, Iterable

from crossing.models import Agent, Invocation, LedgerEvent, Mandate, Outbox, Principal, Receipt, Reservation

FONT = "https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Source+Sans+3:wght@400;600&display=swap"


def _td(v: Any) -> str:
    if v is None:
        return "<td></td>"
    return f"<td>{html.escape(str(v))}</td>"


def _table(title: str, headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(_td(c) for c in row) + "</tr>")
    return (
        f"<section class='panel'><h2>{html.escape(title)}</h2>"
        f"<div class='scroll'><table><tr>{head}</tr>{''.join(body)}</table></div></section>"
    )


def landing() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>The Crossing</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="stylesheet" href="{FONT}"/>
<style>
:root {{
  --ink: #e8e4d9;
  --muted: #b7b0a3;
  --gold: #d4b483;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ height: 100%; }}
body {{
  font-family: "Source Sans 3", system-ui, sans-serif;
  color: var(--ink);
  background: #07080c;
}}
.hero {{
  min-height: 100vh;
  background:
    linear-gradient(180deg, rgba(7,8,12,.35) 0%, rgba(7,8,12,.55) 45%, rgba(7,8,12,.92) 100%),
    url("/static/bridge.jpg") center 40% / cover no-repeat;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  padding: 8vh 8vw 10vh;
}}
h1 {{
  font-family: Cinzel, Palatino, serif;
  font-weight: 700;
  font-size: clamp(3.2rem, 9vw, 7.2rem);
  letter-spacing: .12em;
  line-height: .95;
  text-transform: uppercase;
  text-shadow: 0 8px 40px rgba(0,0,0,.55);
}}
.tag {{
  margin-top: 1.1rem;
  max-width: 34rem;
  font-size: 1.15rem;
  color: var(--muted);
  letter-spacing: .04em;
}}
.gold {{ color: var(--gold); }}
nav {{
  margin-top: 2rem;
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
}}
a.btn {{
  color: #0b0c10;
  background: var(--gold);
  text-decoration: none;
  padding: .7rem 1.15rem;
  letter-spacing: .08em;
  font-weight: 600;
  font-size: .82rem;
  text-transform: uppercase;
}}
a.ghost {{
  color: var(--ink);
  background: transparent;
  border: 1px solid rgba(232,228,217,.35);
}}
footer {{
  margin-top: 2.5rem;
  color: #8a8478;
  font-size: .8rem;
}}
</style>
</head>
<body>
  <main class="hero">
    <h1>The<br>Crossing</h1>
    <p class="tag">Agent economic runtime. Mandates, ledgers, receipts. Authorization, not custody.</p>
    <nav>
      <a class="btn" href="/docs">API</a>
      <a class="btn ghost" href="/dashboard">Operator desk</a>
      <a class="btn ghost" href="https://github.com/dakotalock/the-crossing">Source</a>
    </nav>
    <footer>Invite-only beta. Test-mode Stripe until we say otherwise.</footer>
  </main>
</body>
</html>
"""


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
        aq = aq.filter(Agent.id == None)  # noqa: E711
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
        "<!doctype html><html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width, initial-scale=1'/>",
        "<title>The Crossing — desk</title>",
        f'<link rel="stylesheet" href="{FONT}"/>',
        """<style>
body{margin:0;background:#0c0d12;color:#e8e4d9;font-family:"Source Sans 3",system-ui,sans-serif}
.banner{height:220px;background:linear-gradient(180deg,rgba(12,13,18,.2),#0c0d12),url("/static/bridge.jpg") center 45%/cover;display:flex;align-items:flex-end;padding:1.5rem 2rem}
h1{font-family:Cinzel,Palatino,serif;letter-spacing:.14em;text-transform:uppercase;margin:0;font-size:2rem}
.wrap{padding:1.5rem 2rem 4rem}
.panel{margin:1.4rem 0}
h2{font-family:Cinzel,serif;font-size:.95rem;letter-spacing:.08em;text-transform:uppercase;color:#d4b483}
.scroll{overflow:auto}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border-bottom:1px solid #2a2d36;padding:.45rem .55rem;text-align:left;vertical-align:top}
th{color:#b7b0a3;font-weight:600}
a{color:#d4b483}
</style></head><body>""",
        "<div class='banner'><h1>The Crossing</h1></div><div class='wrap'>",
        "<p>Operator desk. Header key only. No <code>?key=</code>.</p>",
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
        "</div></body></html>",
    ]
    return "\n".join(parts)
