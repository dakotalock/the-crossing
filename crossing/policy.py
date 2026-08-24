"""Deny-by-default policy with stable reason codes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


class Reason:
    MANDATE_FORGED = "MANDATE_FORGED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    NONCE_REPLAY = "NONCE_REPLAY"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    SERVER_NOT_ALLOWED = "SERVER_NOT_ALLOWED"
    CALL_OVER_MAX = "CALL_OVER_MAX"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    AGENT_REVOKED = "AGENT_REVOKED"
    PRINCIPAL_MISSING = "PRINCIPAL_MISSING"
    CHILD_SPEND_ESCALATION = "CHILD_SPEND_ESCALATION"
    CHILD_EXPIRY_ESCALATION = "CHILD_EXPIRY_ESCALATION"
    CHILD_TOOLS_ESCALATION = "CHILD_TOOLS_ESCALATION"
    CHILD_SERVERS_ESCALATION = "CHILD_SERVERS_ESCALATION"
    CHILD_MAX_CALL_ESCALATION = "CHILD_MAX_CALL_ESCALATION"
    CHILD_BUDGET_ESCALATION = "CHILD_BUDGET_ESCALATION"
    MAX_CALLS_EXCEEDED = "MAX_CALLS_EXCEEDED"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    PRODUCTION_SECRETS_MISSING = "PRODUCTION_SECRETS_MISSING"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


@dataclass
class PolicyDenied(Exception):
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        if self.detail:
            return f"{self.reason}: {self.detail}"
        return self.reason


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def subset(child: Iterable[str] | None, parent: Iterable[str] | None) -> bool:
    if parent is None:
        return True
    p = set(parent)
    if not p:
        return child is None or set(child) <= p
    if child is None:
        return False
    return set(child) <= p


def check_agent(agent: Any) -> None:
    if agent is None:
        raise PolicyDenied(Reason.AGENT_REVOKED, "agent missing")
    if getattr(agent, "revoked", False):
        raise PolicyDenied(Reason.AGENT_REVOKED, "agent is revoked")


def check_mandate_signature(mandate: Any, verify_fn) -> None:
    payload = mandate.payload_obj()
    if not verify_fn(payload, mandate.signature, mandate.pubkey_hex):
        raise PolicyDenied(Reason.MANDATE_FORGED, "signature mismatch")


def check_fresh(mandate: Any, now: datetime | None = None) -> None:
    now = as_utc(now or utcnow())
    if mandate.revoked:
        raise PolicyDenied(Reason.MANDATE_REVOKED)
    exp = as_utc(mandate.expires_at)
    if exp is not None and now > exp:
        raise PolicyDenied(Reason.MANDATE_EXPIRED, f"expired at {exp.isoformat()}")


def check_tool(mandate: Any, tool: str, server: str, price_cents: int) -> None:
    tools = mandate.tools_list()
    servers = mandate.servers_list()
    if tools is not None and tool not in tools:
        raise PolicyDenied(Reason.TOOL_NOT_ALLOWED, tool)
    if servers is not None and server not in servers:
        raise PolicyDenied(Reason.SERVER_NOT_ALLOWED, server)
    if price_cents > mandate.max_call_cents:
        raise PolicyDenied(Reason.CALL_OVER_MAX, f"{price_cents} > {mandate.max_call_cents}")
    if mandate.max_calls is not None and mandate.calls_used >= mandate.max_calls:
        raise PolicyDenied(Reason.MAX_CALLS_EXCEEDED)
    if price_cents > mandate.remaining_cents:
        raise PolicyDenied(Reason.BUDGET_EXCEEDED, f"need {price_cents} have {mandate.remaining_cents}")


def check_child_attenuation(
    parent: Any,
    *,
    spend_limit_cents: int,
    max_call_cents: int,
    tools: list[str] | None,
    servers: list[str] | None,
    expires_at: datetime | None,
    max_subagent_budget_cents: int | None,
) -> None:
    if spend_limit_cents > parent.remaining_cents:
        raise PolicyDenied(
            Reason.CHILD_SPEND_ESCALATION,
            f"{spend_limit_cents} > remaining {parent.remaining_cents}",
        )
    if spend_limit_cents > parent.spend_limit_cents:
        raise PolicyDenied(Reason.CHILD_SPEND_ESCALATION)
    if max_call_cents > parent.max_call_cents:
        raise PolicyDenied(Reason.CHILD_MAX_CALL_ESCALATION)
    if not subset(tools, parent.tools_list()):
        raise PolicyDenied(Reason.CHILD_TOOLS_ESCALATION)
    if not subset(servers, parent.servers_list()):
        raise PolicyDenied(Reason.CHILD_SERVERS_ESCALATION)
    parent_exp = as_utc(parent.expires_at)
    child_exp = as_utc(expires_at)
    if parent_exp is not None and (child_exp is None or child_exp > parent_exp):
        raise PolicyDenied(Reason.CHILD_EXPIRY_ESCALATION)
    cap = parent.remaining_cents
    if parent.max_subagent_budget_cents is not None:
        cap = min(cap, parent.max_subagent_budget_cents)
    child_sub = max_subagent_budget_cents if max_subagent_budget_cents is not None else spend_limit_cents
    if child_sub > cap:
        raise PolicyDenied(
            Reason.CHILD_BUDGET_ESCALATION,
            f"{child_sub} > remaining/cap {cap}",
        )
