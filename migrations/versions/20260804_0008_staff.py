"""create the staff tables

Plan 0005 PR 7 (migration #7 of §8.2): employees, work shifts, payroll rates and
attendance records. No enum of its own — the module has nothing to enumerate.

Revision ID: 20260804_0008
Revises: 20260804_0007
Create Date: 2026-08-04 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0008"
down_revision: str | Sequence[str] | None = "20260804_0007"
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


def _timestamps(*, updated: bool = True) -> list[sa.Column]:
    columns = [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
        )
    return columns


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_employees"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_employees_user_id_users",
            ondelete="SET NULL",
        ),
        # Nullable and unique: at most one employee per account, and most
        # employees have no account at all (D6).
        sa.UniqueConstraint("user_id", name="uq_employees_user_id"),
    )
    op.create_index("ix_employees_sync_seq", "employees", ["sync_seq"])

    op.create_table(
        "work_shifts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_work_shifts"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_work_shifts_ends_after_start"),
    )
    op.create_index("ix_work_shifts_code", "work_shifts", ["code"], unique=True)
    op.create_index("ix_work_shifts_sync_seq", "work_shifts", ["sync_seq"])

    op.create_table(
        "payroll_rates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        *_syncable(),
        *_timestamps(updated=False),
        sa.PrimaryKeyConstraint("id", name="pk_payroll_rates"),
        sa.CheckConstraint("amount > 0", name="ck_payroll_rates_amount_positive"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_payroll_rates_window_ordered"
        ),
    )
    op.create_index("ix_payroll_rates_code_valid_from", "payroll_rates", ["code", "valid_from"])
    op.create_index("ix_payroll_rates_sync_seq", "payroll_rates", ["sync_seq"])

    op.create_table(
        "attendance_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=True),
        sa.Column("clock_in", sa.Time(), nullable=False),
        sa.Column("clock_out", sa.Time(), nullable=True),
        sa.Column("overtime_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_attendance_records"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name="fk_attendance_records_employee_id_employees"
        ),
        sa.ForeignKeyConstraint(
            ["shift_id"], ["work_shifts.id"], name="fk_attendance_records_shift_id_work_shifts"
        ),
        sa.CheckConstraint(
            "clock_out IS NULL OR clock_out > clock_in",
            name="ck_attendance_records_out_after_in",
        ),
        sa.CheckConstraint(
            "overtime_minutes >= 0", name="ck_attendance_records_overtime_not_negative"
        ),
    )
    op.create_index("ix_attendance_records_employee_id", "attendance_records", ["employee_id"])
    op.create_index("ix_attendance_records_work_date", "attendance_records", ["work_date"])
    op.create_index("ix_attendance_records_shift_id", "attendance_records", ["shift_id"])
    op.create_index(
        "ix_attendance_records_employee_work_date",
        "attendance_records",
        ["employee_id", "work_date"],
    )
    op.create_index("ix_attendance_records_sync_seq", "attendance_records", ["sync_seq"])


def downgrade() -> None:
    op.drop_table("attendance_records")
    op.drop_table("payroll_rates")
    op.drop_table("work_shifts")
    op.drop_table("employees")
