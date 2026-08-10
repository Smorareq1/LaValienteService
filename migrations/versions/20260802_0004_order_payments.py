"""create the order payments table

Plan 0001 PR 4: the rest of the life cycle — states, delivery and cancellation —
writes into columns `20260802_0003` already created, so the only new structure is
where money received against a ticket is written down.

Revision ID: 20260802_0004
Revises: 20260802_0003
Create Date: 2026-08-02 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0004"
down_revision: str | Sequence[str] | None = "20260802_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_SEQ = "sync_seq_global"


def upgrade() -> None:
    payment_method = postgresql.ENUM("cash", "transfer", name="payment_method", create_type=False)
    payment_method.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "order_payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("method", payment_method, nullable=False),
        sa.Column("is_advance", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reference", sa.String(length=80), nullable=True),
        sa.Column("received_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "paid_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "sync_seq",
            sa.BigInteger(),
            server_default=sa.text(f"nextval('{SYNC_SEQ}')"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_payments"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_payments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["received_by_id"], ["users.id"], name="fk_order_payments_received_by_id_users"
        ),
        sa.CheckConstraint("amount > 0", name="ck_order_payments_amount_positive"),
    )
    op.create_index("ix_order_payments_order_id", "order_payments", ["order_id"])
    op.create_index("ix_order_payments_sync_seq", "order_payments", ["sync_seq"])


def downgrade() -> None:
    op.drop_table("order_payments")
    postgresql.ENUM(name="payment_method").drop(op.get_bind(), checkfirst=True)
