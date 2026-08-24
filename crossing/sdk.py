"""In-process Crossing client."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from crossing import crypto, db
from crossing.identity import create_agent, create_principal, create_task, revoke_agent
from crossing.lifecycle import invoke as lc_invoke
from crossing.lifecycle import quote as lc_quote
from crossing.mandate import issue_mandate, load_live_mandate
from crossing.models import Agent, Mandate, Principal, Receipt, Task
from crossing.receipts import to_dict, verify_receipt
from crossing.policy import PolicyDenied


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

    def get_mandate(self, mandate_id: str) -> Mandate:
        with db.session_scope() as s:
            return load_live_mandate(s, mandate_id)

    def verify_receipt(self, receipt: Receipt | dict[str, Any]) -> bool:
        return verify_receipt(receipt)

    def receipt_dict(self, receipt_id: str) -> dict[str, Any] | None:
        with db.session_scope() as s:
            r = s.get(Receipt, receipt_id)
            return to_dict(r) if r else None

    def session(self):
        return db.session_scope()
