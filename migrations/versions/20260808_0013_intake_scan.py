"""create the scan jobs table

Plan 0003 PR A: one row per photograph of a paper ticket, with what the model
read, what we made of it, and — once somebody confirms the draft — the ticket it
became and the diff of everything they had to correct.

Two things this migration deliberately does **not** do, both of them D2. It adds
no column to `orders`: the reference goes `scan_jobs.order_id → orders`, so the
core never learns this module exists. And the table carries no `sync_seq`,
`version` or `deleted_at` — a scan is not mirrored on the devices, so it is not
part of the change feed. The day the shop drops the paper booklet, the retirement
is `drop table scan_jobs` and deleting a directory.

Revision ID: 20260808_0013
Revises: 20260805_0012
Create Date: 2026-08-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260808_0013"
down_revision: str | Sequence[str] | None = "20260805_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# postgresql.ENUM y no sa.Enum: `create_type` es del dialecto de Postgres y el
# tipo genérico lo ignora, así que la columna volvería a emitir CREATE TYPE al
# crear la tabla y chocaría con el de abajo. Misma forma que 0001, 0002 y 0003.
SCAN_STATUS = postgresql.ENUM(
    "processing", "completed", "failed", name="scan_status", create_type=False
)


def upgrade() -> None:
    SCAN_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "scan_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", SCAN_STATUS, nullable=False),
        sa.Column("image_path", sa.String(length=500), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        # JSONB and not JSON: the corrections of D7 are queried by key to answer
        # "which fields get corrected most", and that needs an operator class.
        sa.Column("raw_response", postgresql.JSONB(), nullable=True),
        sa.Column("extracted", postgresql.JSONB(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("corrections", postgresql.JSONB(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_scan_jobs"),
        # SET NULL and not CASCADE: a scan is evidence of what the model read
        # that day, and it outlives the ticket it produced.
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_scan_jobs_order_id_orders",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_scan_jobs_created_by_id_users"
        ),
    )
    op.create_index("ix_scan_jobs_created_at", "scan_jobs", ["created_at"])
    op.create_index("ix_scan_jobs_order_id", "scan_jobs", ["order_id"])
    op.create_index("ix_scan_jobs_status", "scan_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_scan_jobs_status", table_name="scan_jobs")
    op.drop_index("ix_scan_jobs_order_id", table_name="scan_jobs")
    op.drop_index("ix_scan_jobs_created_at", table_name="scan_jobs")
    op.drop_table("scan_jobs")
    postgresql.ENUM(name="scan_status").drop(op.get_bind(), checkfirst=True)
