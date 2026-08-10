"""create the expense tables

Plan 0005 PR 9 (migration #9 of §8.2): administrable categories and the day's
expenses, with their links to the employee, the working day and the lot that
each one paid for.

The `expense_status` enum is new. `payment_method` is reused with
`create_type=False` (D13) — it was born in the orders migration.

Revision ID: 20260805_0010
Revises: 20260805_0009
Create Date: 2026-08-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_0010"
down_revision: str | Sequence[str] | None = "20260805_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_SEQ = "sync_seq_global"


def _syncable() -> list[sa.Column]:
    """The three feed columns every synchronizable table carries (§8.2)."""
    return [
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "sync_seq",
            sa.BigInteger(),
            server_default=sa.text(f"nextval('{SYNC_SEQ}')"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    expense_status = postgresql.ENUM(
        "paid", "pending", name="expense_status", create_type=False
    )
    expense_status.create(op.get_bind(), checkfirst=True)

    payment_method = postgresql.ENUM(
        "cash", "transfer", name="payment_method", create_type=False
    )

    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_expense_categories"),
        sa.UniqueConstraint("name", name="uq_expense_categories_name"),
    )
    op.create_index("ix_expense_categories_sync_seq", "expense_categories", ["sync_seq"])

    op.create_table(
        "expenses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("concept", sa.String(length=160), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("method", payment_method, nullable=False),
        sa.Column("status", expense_status, nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("attendance_record_id", sa.Uuid(), nullable=True),
        sa.Column("product_lot_id", sa.Uuid(), nullable=True),
        sa.Column("observations", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("voided_by_id", sa.Uuid(), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_expenses"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["expense_categories.id"],
            name="fk_expenses_category_id_expense_categories",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name="fk_expenses_employee_id_employees"
        ),
        sa.ForeignKeyConstraint(
            ["attendance_record_id"],
            ["attendance_records.id"],
            name="fk_expenses_attendance_record_id_attendance_records",
        ),
        sa.ForeignKeyConstraint(
            ["product_lot_id"], ["product_lots.id"], name="fk_expenses_product_lot_id_product_lots"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_expenses_created_by_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["voided_by_id"], ["users.id"], name="fk_expenses_voided_by_id_users"
        ),
        sa.CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
    )
    op.create_index("ix_expenses_expense_date", "expenses", ["expense_date"])
    op.create_index("ix_expenses_category_id", "expenses", ["category_id"])
    op.create_index("ix_expenses_status", "expenses", ["status"])
    op.create_index("ix_expenses_employee_id", "expenses", ["employee_id"])
    op.create_index("ix_expenses_product_lot_id", "expenses", ["product_lot_id"])
    op.create_index("ix_expenses_sync_seq", "expenses", ["sync_seq"])
    # One live payment per working day (§6.2 step 4). Partial, so voiding one
    # frees the day to be paid properly.
    op.create_index(
        "uq_expenses_attendance_record",
        "expenses",
        ["attendance_record_id"],
        unique=True,
        postgresql_where=sa.text("attendance_record_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("expenses")
    op.drop_table("expense_categories")
    postgresql.ENUM(name="expense_status").drop(op.get_bind(), checkfirst=True)
