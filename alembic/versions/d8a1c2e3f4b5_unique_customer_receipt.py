"""unique stripe_customer_id and receipts.reservation_id

Revision ID: d8a1c2e3f4b5
Revises: c4e91a7b2d10
Create Date: 2026-08-24 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d8a1c2e3f4b5"
down_revision: Union[str, Sequence[str], None] = "c4e91a7b2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_accounts_stripe_customer_id", table_name="accounts")
    op.create_index(
        "uq_accounts_stripe_customer_id",
        "accounts",
        ["stripe_customer_id"],
        unique=True,
    )
    op.create_index(
        "uq_receipts_reservation_id",
        "receipts",
        ["reservation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_receipts_reservation_id", table_name="receipts")
    op.drop_index("uq_accounts_stripe_customer_id", table_name="accounts")
    op.create_index("ix_accounts_stripe_customer_id", "accounts", ["stripe_customer_id"], unique=False)
