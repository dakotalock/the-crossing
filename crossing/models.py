from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    pubkey_hex: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agents: Mapped[list[Agent]] = relationship(back_populates="principal")
    mandates: Mapped[list[Mandate]] = relationship(back_populates="principal")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    principal: Mapped[Principal] = relationship(back_populates="agents")
    parent: Mapped[Agent | None] = relationship(remote_side="Agent.id")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="task")
    status: Mapped[str] = mapped_column(String(40), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Mandate(Base):
    __tablename__ = "mandates"
    __table_args__ = (
        UniqueConstraint("nonce", name="uq_mandate_nonce"),
        CheckConstraint("spend_limit_cents >= 0", name="ck_mandate_spend_nonneg"),
        CheckConstraint("remaining_cents >= 0", name="ck_mandate_remaining_nonneg"),
        CheckConstraint("max_call_cents >= 0", name="ck_mandate_max_call_nonneg"),
        CheckConstraint(
            "max_subagent_budget_cents IS NULL OR max_subagent_budget_cents >= 0",
            name="ck_mandate_subagent_nonneg",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    parent_mandate_id: Mapped[str | None] = mapped_column(ForeignKey("mandates.id"), nullable=True)
    spend_limit_cents: Mapped[int] = mapped_column(Integer)
    remaining_cents: Mapped[int] = mapped_column(Integer)
    max_call_cents: Mapped[int] = mapped_column(Integer)
    max_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)
    tools_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    servers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_subagent_budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nonce: Mapped[str] = mapped_column(String(80), index=True)
    signature: Mapped[str] = mapped_column(Text)
    pubkey_hex: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    principal: Mapped[Principal] = relationship(back_populates="mandates")

    def tools_list(self) -> list[str] | None:
        if not self.tools_json:
            return None
        return json.loads(self.tools_json)

    def servers_list(self) -> list[str] | None:
        if not self.servers_json:
            return None
        return json.loads(self.servers_json)

    def payload_obj(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


class LedgerEvent(Base):
    __tablename__ = "ledger_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    mandate_id: Mapped[str] = mapped_column(String(36), index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)  # reserve|commit|release|deny|escrow|unescrow
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    remaining_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (CheckConstraint("amount_cents >= 0", name="ck_reservation_amount_nonneg"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="held")  # held|committed|released
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    nonce: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    mandate_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool: Mapped[str] = mapped_column(String(80))
    server: Mapped[str] = mapped_column(String(80))
    amount_cents: Mapped[int] = mapped_column(Integer)
    body_json: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    pubkey_hex: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def body_obj(self) -> dict[str, Any]:
        return json.loads(self.body_json)


class Outbox(Base):
    """billing_outbox: pending Stripe (or noop) work. Never HTTP inside the ledger txn."""

    __tablename__ = "billing_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40), default="stripe_meter")
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed|noop
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("principal_id", "idempotency_key", name="uq_idem_principal_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # LogicalOperation: unique (principal_id, idempotency_key)
    # in_progress | completed — claim is the at-most-one gate for a logical op
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsedNonce(Base):
    __tablename__ = "used_nonces"
    __table_args__ = (UniqueConstraint("principal_id", "nonce", name="uq_principal_nonce"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    nonce: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invocation(Base):
    """ExecutionAttempt. Multiple attempts may share (principal_id, idempotency_key)."""

    __tablename__ = "invocations"
    __table_args__ = (
        Index("ix_invocation_principal_idempotency_key", "principal_id", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(36), index=True)
    mandate_id: Mapped[str] = mapped_column(String(36), index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool: Mapped[str] = mapped_column(String(80), default="")
    server: Mapped[str] = mapped_column(String(80), default="")
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    # in_progress | reserved | executed_ok | executed_fail | committed | released | ambiguous
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
