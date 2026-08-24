"""Crossing SDK: in-process runtime and HTTP /v1 client."""

from __future__ import annotations

import os
from typing import Any

import httpx

from crossing import billing, crypto, db
from crossing.identity import create_agent, create_principal, create_task, revoke_agent
from crossing.lifecycle import invoke as lc_invoke
from crossing.lifecycle import quote as lc_quote
from crossing.mandate import issue_mandate, load_live_mandate
from crossing.models import Account, Agent, Mandate, Principal, Receipt, Task
from crossing.policy import PolicyDenied
from crossing.receipts import to_dict, verify_receipt


class Crossing:
    def __init__(self, database_url: str | None = None) -> None:
        if os.environ.get("CROSSING_ALLOW_DEV") != "1":
            crypto.require_production_secrets()
        db.reset_engine()
        db.init_db(database_url or os.environ.get("DATABASE_URL") or db.DEFAULT_URL)

    @classmethod
    def in_process(cls, database_url: str = "sqlite:///:memory:") -> Crossing:
        os.environ.setdefault("CROSSING_ALLOW_DEV", "1")
        return cls(database_url=database_url)

    def create_principal(self, name: str, pubkey_hex: str | None = None) -> Principal:
        with db.session_scope() as s:
            return create_principal(s, name, pubkey_hex=pubkey_hex)

    def create_agent(self, principal_id: str, name: str, parent_id: str | None = None) -> Agent:
        with db.session_scope() as s:
            return create_agent(s, principal_id, name, parent_id=parent_id)

    def revoke_agent(self, agent_id: str) -> Agent:
        with db.session_scope() as s:
            return revoke_agent(s, agent_id)

    def create_task(self, principal_id: str, agent_id: str | None, name: str = "task") -> Task:
        with db.session_scope() as s:
            return create_task(s, principal_id, agent_id, name)

    def issue_mandate(
        self,
        principal_id: str,
        agent_id: str,
        spend_limit_cents: int,
        **kwargs: Any,
    ) -> Mandate:
        with db.session_scope() as s:
            return issue_mandate(s, principal_id=principal_id, agent_id=agent_id, spend_limit_cents=spend_limit_cents, **kwargs)

    def attenuate(self, parent_mandate_id: str, agent_id: str, spend_limit_cents: int, **kwargs: Any) -> Mandate:
        with db.session_scope() as s:
            parent = s.get(Mandate, parent_mandate_id)
            if parent is None:
                raise PolicyDenied("MANDATE_REVOKED", "parent missing")
            return issue_mandate(
                s,
                principal_id=parent.principal_id,
                agent_id=agent_id,
                spend_limit_cents=spend_limit_cents,
                parent_mandate_id=parent_mandate_id,
                **kwargs,
            )

    def quote(self, tool: str, server: str = "mock") -> int:
        return lc_quote(tool, server)

    def invoke(
        self,
        mandate_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        with db.session_scope() as s:
            return lc_invoke(s, mandate_id=mandate_id, tool=tool, arguments=arguments, **kwargs)

    def remaining(self, mandate_id: str) -> int:
        with db.session_scope() as s:
            m = s.get(Mandate, mandate_id)
            if m is None:
                raise KeyError(mandate_id)
            return m.remaining_cents

    remaining_budget = remaining

    def get_mandate(self, mandate_id: str) -> Mandate:
        with db.session_scope() as s:
            return load_live_mandate(s, mandate_id)

    def verify_receipt(self, receipt: Receipt | dict[str, Any]) -> bool:
        return verify_receipt(receipt)

    def receipt_dict(self, receipt_id: str) -> dict[str, Any] | None:
        with db.session_scope() as s:
            r = s.get(Receipt, receipt_id)
            return to_dict(r) if r else None

    get_receipt = receipt_dict

    def account(self, principal_id: str | None = None) -> dict[str, Any]:
        with db.session_scope() as s:
            if principal_id:
                p = s.get(Principal, principal_id)
                if p is None:
                    raise KeyError(principal_id)
                acct = s.get(Account, p.account_id)
            else:
                acct = s.query(Account).first()
            if acct is None:
                raise KeyError("account")
            return {
                "account_id": acct.id,
                "name": acct.name,
                "stripe_customer_present": bool(acct.stripe_customer_id),
            }

    def billing_status(self, account_id: str) -> dict[str, Any]:
        with db.session_scope() as s:
            return billing.billing_status(s, account_id)

    def session(self):
        return db.session_scope()


class CrossingClient:
    """HTTP client for /v1. Authenticate with X-API-Key."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self._http.request(method, path, **kwargs)
        r.raise_for_status()
        if r.content:
            return r.json()
        return None

    def authenticate(self) -> dict[str, Any]:
        return self.account()

    def account(self) -> dict[str, Any]:
        return self._req("GET", "/v1/account")

    def create_principal(self, name: str, pubkey_hex: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if pubkey_hex:
            body["pubkey_hex"] = pubkey_hex
        return self._req("POST", "/v1/principals", json=body)

    def create_agent(self, principal_id: str, name: str, parent_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"principal_id": principal_id, "name": name}
        if parent_id:
            body["parent_id"] = parent_id
        return self._req("POST", "/v1/agents", json=body)

    def issue_mandate(
        self,
        principal_id: str,
        agent_id: str,
        spend_limit_cents: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = {"principal_id": principal_id, "agent_id": agent_id, "spend_limit_cents": spend_limit_cents, **kwargs}
        return self._req("POST", "/v1/mandates", json=body)

    def invoke(
        self,
        mandate_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        server: str = "mock",
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = {
            "mandate_id": mandate_id,
            "tool": tool,
            "arguments": arguments or {},
            "server": server,
            **kwargs,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return self._req("POST", "/v1/invoke", json=body)

    def get_receipt(self, receipt_id: str) -> dict[str, Any]:
        return self._req("GET", f"/v1/receipts/{receipt_id}")

    def verify_receipt(self, receipt: dict[str, Any]) -> bool:
        return verify_receipt(receipt)

    def remaining_budget(self, mandate_id: str) -> int:
        data = self._req("GET", f"/v1/mandates/{mandate_id}")
        return int(data["remaining_cents"])

    remaining = remaining_budget

    def billing_status(self) -> dict[str, Any]:
        return self._req("GET", "/v1/billing/status")
