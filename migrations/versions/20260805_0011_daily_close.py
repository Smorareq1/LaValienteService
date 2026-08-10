"""create the daily closure table

Plan 0005 PR 10 (migration #10 of §8.2): the day's record of account, with the
arqueo and the trail a reopening leaves behind.

No new enum and no new foreign keys beyond `users`: the close reads payments,
sales and expenses, but it does not point at them — it is the snapshot of what
they said (D9).

Revision ID: 20260805_0011
Revises: 20260805_0010
Create Date: 2026-08-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0011"
down_revision: str | Sequence[str] | None = "20260805_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_SEQ = "sync_seq_global"

MONEY = sa.Numeric(precision=10, scale=2)


def _money(name: str) -> sa.Column:
    return sa.Column(name, MONEY, nullable=False)


def upgrade() -> None:
    op.create_table(
        "daily_closures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("close_date", sa.Date(), nullable=False),
        _money("orders_income"),
        _money("supplies_income"),
        _money("expenses_total"),
        _money("net_total"),
        _money("cash_income"),
        _money("transfer_income"),
        _money("cash_expenses"),
        _money("transfer_expenses"),
        sa.Column(
            "orders_delivered", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("closed_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "closed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("reopened_by_id", sa.Uuid(), nullable=True),
        sa.Column("reopen_reason", sa.Text(), nullable=True),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_daily_closures"),
        sa.ForeignKeyConstraint(
            ["closed_by_id"], ["users.id"], name="fk_daily_closures_closed_by_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["reopened_by_id"], ["users.id"], name="fk_daily_closures_reopened_by_id_users"
        ),
    )
    op.create_index("ix_daily_closures_sync_seq", "daily_closures", ["sync_seq"])
    # One live close per date. Partial, so reopening a day (a tombstone, D9)
    # frees it to be closed again while the undone one stays on the record.
    op.create_index(
        "uq_daily_closures_close_date",
        "daily_closures",
        ["close_date"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("daily_closures")
