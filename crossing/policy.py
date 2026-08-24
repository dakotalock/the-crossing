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
    SIGNED_STATE_DIVERGED = "SIGNED_STATE_DIVERGED"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AGENT_PRINCIPAL_MISMATCH = "AGENT_PRINCIPAL_MISMATCH"
    CHILD_AGENT_NOT_DESCENDANT = "CHILD_AGENT_NOT_DESCENDANT"
    UNAUTHORIZED = "UNAUTHORIZED"


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


def check_non_negative_money(**amounts: int | None) -> None:
    for name, value in amounts.items():
        if value is None:
            continue
        if int(value) < 0:
            raise PolicyDenied(Reason.INVALID_AMOUNT, f"{name} must be >= 0")


def _norm_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    return sorted(v)


def check_signed_state(mandate: Any) -> None:
    """Compare immutable signed payload to enforcement columns.

    remaining_cents and calls_used (used_calls) are mutable accounting and MUST NOT
    appear in the signed payload. remaining starts equal to signed spend_limit.
    """
    from crossing.mandate import mandate_payload

    signed = mandate.payload_obj()
    if "remaining_cents" in signed or "calls_used" in signed or "used_calls" in signed:
        raise PolicyDenied(Reason.SIGNED_STATE_DIVERGED, "mutable accounting in signed payload")
    rebuilt = mandate_payload(
        principal_id=mandate.principal_id,
        agent_id=mandate.agent_id,
        parent_mandate_id=mandate.parent_mandate_id,
        spend_limit_cents=mandate.spend_limit_cents,
        max_call_cents=mandate.max_call_cents,
        max_calls=mandate.max_calls,
        tools=mandate.tools_list(),
        servers=mandate.servers_list(),
        expires_at=mandate.expires_at,
        max_subagent_budget_cents=mandate.max_subagent_budget_cents,
        nonce=mandate.nonce,
    )
    keys = (
        "principal_id",
        "agent_id",
        "parent_mandate_id",
        "spend_limit_cents",
        "max_call_cents",
        "max_calls",
        "tools",
        "servers",
        "expires_at",
        "max_subagent_budget_cents",
        "nonce",
    )
    for k in keys:
        sv, rv = signed.get(k), rebuilt.get(k)
        if k in ("tools", "servers"):
            if _norm_list(sv) != _norm_list(rv):
                raise PolicyDenied(Reason.SIGNED_STATE_DIVERGED, k)
        elif k == "expires_at":
            if (sv or None) != (rv or None):
                raise PolicyDenied(Reason.SIGNED_STATE_DIVERGED, k)
        elif sv != rv:
            raise PolicyDenied(Reason.SIGNED_STATE_DIVERGED, k)


def bound_pubkeys(principal: Any, issuer_hex: str) -> set[str]:
    keys = {issuer_hex}
    stored = getattr(principal, "pubkey_hex", None) if principal is not None else None
    if stored:
        keys.add(stored)
    return keys


def check_mandate_signature(mandate: Any, verify_fn, *, allowed_pubkeys: set[str] | None = None) -> None:
    payload = mandate.payload_obj()
    key = mandate.pubkey_hex
    if allowed_pubkeys is not None and key not in allowed_pubkeys:
        raise PolicyDenied(Reason.MANDATE_FORGED, "pubkey is not a registered trust anchor")
    if not verify_fn(payload, mandate.signature, key):
        raise PolicyDenied(Reason.MANDATE_FORGED, "signature mismatch")


def check_fresh(mandate: Any, now: datetime | None = None) -> None:
    now = as_utc(now or utcnow())
    if mandate.revoked:
        raise PolicyDenied(Reason.MANDATE_REVOKED)
    exp = as_utc(mandate.expires_at)
    if exp is not None and now > exp:
        raise PolicyDenied(Reason.MANDATE_EXPIRED, f"expired at {exp.isoformat()}")


def check_tool(mandate: Any, tool: str, server: str, price_cents: int) -> None:
    check_signed_state(mandate)
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
    if spend_limit_cents <= 0:
        raise PolicyDenied(Reason.INVALID_AMOUNT, "child spend must be > 0")
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
    if child_sub is not None and child_sub < 0:
        raise PolicyDenied(Reason.INVALID_AMOUNT, "max_subagent_budget_cents must be >= 0")
    if child_sub > cap:
        raise PolicyDenied(
            Reason.CHILD_BUDGET_ESCALATION,
            f"{child_sub} > remaining/cap {cap}",
        )
