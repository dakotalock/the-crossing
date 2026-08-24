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


class Account(Base):
    """Tenant. v1 is 1:1 with Principal; API keys hang off Account."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("stripe_customer_id", name="uq_accounts_stripe_customer_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stripe_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fee_microcents: Mapped[int] = mapped_column(Integer, default=0)
    fee_invoiced_cents: Mapped[int] = mapped_column(Integer, default=0)

    principals: Mapped[list[Principal]] = relationship(back_populates="account")
    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="account")


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    pubkey_hex: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="principals")
    agents: Mapped[list[Agent]] = relationship(back_populates="principal")
    mandates: Mapped[list[Mandate]] = relationship(back_populates="principals")
