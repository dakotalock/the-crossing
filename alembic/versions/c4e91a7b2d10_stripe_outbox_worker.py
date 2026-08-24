"""stripe customer, unique outbox receipt, stripe_events

Revision ID: c4e91a7b2d10
Revises: b38032f6388f
Create Date: 2026-08-24 12:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e91a7b2d10"
down_revision: Union[str, Sequence[str], None] = "b38032f6388f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("stripe_customer_id", sa.String(length=80), nullable=True))
    op.add_column("accounts", sa.Column("stripe_subscription_id", sa.String(length=80), nullable=True))
    op.add_column("accounts", sa.Column("stripe_price_id", sa.String(length=80), nullable=True))
    op.add_column("accounts", sa.Column("stripe_status", sa.String(length=40), nullable=True))
    op.add_column("accounts", sa.Column("fee_microcents", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("accounts", sa.Column("fee_invoiced_cents", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_accounts_stripe_customer_id", "accounts", ["stripe_customer_id"], unique=False)

    op.add_column("billing_outbox", sa.Column("receipt_id", sa.String(length=36), nullable=True))
    op.add_column("billing_outbox", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_billing_outbox_receipt_id", "billing_outbox", ["receipt_id"], unique=True)
    op.create_index("ix_billing_outbox_status", "billing_outbox", ["status"], unique=False)

    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_stripe_events_account_id", "stripe_events", ["account_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stripe_events_account_id", table_name="stripe_events")
    op.drop_table("stripe_events")
    op.drop_index("ix_billing_outbox_status", table_name="billing_outbox")
    op.drop_index("ix_billing_outbox_receipt_id", table_name="billing_outbox")
    op.drop_column("billing_outbox", "claimed_at")
    op.drop_column("billing_outbox", "receipt_id")
    op.drop_index("ix_accounts_stripe_customer_id", table_name="accounts")
    op.drop_column("accounts", "fee_invoiced_cents")
    op.drop_column("accounts", "fee_microcents")
    op.drop_column("accounts", "stripe_status")
    op.drop_column("accounts", "stripe_price_id")
    op.drop_column("accounts", "stripe_subscription_id")
    op.drop_column("accounts", "stripe_customer_id")
