"""In-process counters for beta. Not multi-host."""

from __future__ import annotations

from collections import Counter
from typing import Any

_invokes = 0
_denials: Counter[str] = Counter()


def reset_for_tests() -> None:
    global _invokes
    _invokes = 0
    _denials.clear()


def inc_invoke() -> None:
    global _invokes
    _invokes += 1


def inc_deny(reason: str) -> None:
    _denials[reason or "unknown"] += 1


def snapshot() -> dict[str, Any]:
    from crossing import db
    from crossing.models import Outbox

    backlog = 0
    dead = 0
    if db.SessionLocal is not None:
        with db.session_scope() as s:
            backlog = (
                s.query(Outbox)
                .filter(Outbox.status.in_(("pending", "failed", "sending")))
                .count()
            )
            dead = s.query(Outbox).filter(Outbox.status == "dead").count()
    return {
        "invokes_total": _invokes,
        "denials_by_reason": dict(_denials),
        "outbox_backlog": backlog,
        "outbox_dead": dead,
    }


def prometheus_text() -> str:
    data = snapshot()
    lines = [
        "# TYPE crossing_invokes_total counter",
        f"crossing_invokes_total {data['invokes_total']}",
        "# TYPE crossing_outbox_backlog gauge",
        f"crossing_outbox_backlog {data['outbox_backlog']}",
        "# TYPE crossing_outbox_dead gauge",
        f"crossing_outbox_dead {data['outbox_dead']}",
        "# TYPE crossing_denials_total counter",
    ]
    denials = data["denials_by_reason"]
    if not denials:
        lines.append('crossing_denials_total{reason="none"} 0')
    else:
        for reason, n in sorted(denials.items()):
            safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in reason)[:64]
            lines.append(f'crossing_denials_total{{reason="{safe}"}} {n}')
    return "\n".join(lines) + "\n"
