"""create catalog, customers and the synchronization core

Adds the first synchronizable modules (Plan 0001 PR 1-2) on top of the change-feed
machinery of Plan 0004 PR S1: a single global sequence orders every write, and
every synchronizable table carries `version` / `sync_seq` / `deleted_at`.

Revision ID: 20260731_0002
Revises: 20260714_0001
Create Date: 2026-07-31 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_0002"
down_revision: str | Sequence[str] | None = "20260714_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_SEQ = "sync_seq_global"


def _syncable_columns() -> list[sa.Column]:
    """The three columns every synchronizable table carries (Plan 0004 §6.1)."""
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


def _timestamp_columns() -> list[sa.Column]:
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
    # The single sequence behind the whole change feed. Created before any table
    # so their sync_seq defaults can reference it.
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {SYNC_SEQ} AS bigint START 1"))

    pricing_mode = postgresql.ENUM(
        "per_unit", "tiered", "variable", name="pricing_mode", create_type=False
    )
    pricing_mode.create(op.get_bind(), checkfirst=True)
    operation_status = postgresql.ENUM(
        "applied",
        "already_applied",
        "rejected",
        "conflict",
        name="sync_operation_status",
        create_type=False,
    )
    operation_status.create(op.get_bind(), checkfirst=True)

    # -- customers -----------------------------------------------------
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("nit", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_syncable_columns(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
    )
    op.create_index("ix_customers_phone", "customers", ["phone"])
    op.create_index("ix_customers_sync_seq", "customers", ["sync_seq"])
    # Search normalizes casing, so the index has to as well or it goes unused.
    op.create_index("ix_customers_full_name_lower", "customers", [sa.text("lower(full_name)")])

    # -- catalog -------------------------------------------------------
    op.create_table(
        "service_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("pricing_mode", pricing_mode, nullable=False),
        sa.Column("unit_label", sa.String(length=30), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable_columns(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_service_types"),
        sa.UniqueConstraint("code", name="uq_service_types_code"),
    )
    op.create_index("ix_service_types_code", "service_types", ["code"])
    op.create_index("ix_service_types_sync_seq", "service_types", ["sync_seq"])

    op.create_table(
        "service_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_type_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("min_quantity", sa.Integer(), nullable=True),
        sa.Column("max_quantity", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable_columns(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_service_options"),
        sa.ForeignKeyConstraint(
            ["service_type_id"],
            ["service_types.id"],
            name="fk_service_options_service_type_id_service_types",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("service_type_id", "code", name="uq_service_options_service_type_id"),
    )
    op.create_index("ix_service_options_service_type_id", "service_options", ["service_type_id"])
    op.create_index("ix_service_options_sync_seq", "service_options", ["sync_seq"])

    op.create_table(
        "service_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_type_id", sa.Uuid(), nullable=False),
        sa.Column("service_option_id", sa.Uuid(), nullable=True),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        *_syncable_columns(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_prices"),
        sa.ForeignKeyConstraint(
            ["service_type_id"],
            ["service_types.id"],
            name="fk_service_prices_service_type_id_service_types",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_option_id"],
            ["service_options.id"],
            name="fk_service_prices_service_option_id_service_options",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_service_prices_valid_window",
        ),
    )
    op.create_index("ix_service_prices_service_type_id", "service_prices", ["service_type_id"])
    op.create_index("ix_service_prices_service_option_id", "service_prices", ["service_option_id"])
    op.create_index("ix_service_prices_valid_from", "service_prices", ["valid_from"])
    op.create_index("ix_service_prices_sync_seq", "service_prices", ["sync_seq"])
    # At most one open window per service/option — two would make a ticket's total
    # depend on which row the query happened to return first.
    op.create_index(
        "uq_service_prices_open_window",
        "service_prices",
        ["service_type_id", "service_option_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "garment_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable_columns(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_garment_types"),
        sa.UniqueConstraint("name", name="uq_garment_types_name"),
    )
    op.create_index("ix_garment_types_sync_seq", "garment_types", ["sync_seq"])

    # -- sync ----------------------------------------------------------
    op.create_table(
        "sync_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=True),
        sa.Column("app_version", sa.String(length=40), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pull_cursor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_devices"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sync_devices_user_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_sync_devices_user_id", "sync_devices", ["user_id"])

    op.create_table(
        "sync_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("op_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("entity", sa.String(length=40), nullable=False),
        sa.Column("op_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", operation_status, nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("client_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_operations"),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["sync_devices.id"],
            name="fk_sync_operations_device_id_sync_devices",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sync_operations_user_id_users", ondelete="CASCADE"
        ),
        # The idempotency guarantee itself: a replayed op_id cannot insert twice.
        sa.UniqueConstraint("op_id", name="uq_sync_operations_op_id"),
    )
    op.create_index("ix_sync_operations_op_id", "sync_operations", ["op_id"])
    op.create_index("ix_sync_operations_device_id", "sync_operations", ["device_id"])
    op.create_index("ix_sync_operations_user_id", "sync_operations", ["user_id"])
    op.create_index("ix_sync_operations_entity_id", "sync_operations", ["entity_id"])


def downgrade() -> None:
    op.drop_table("sync_operations")
    op.drop_table("sync_devices")
    op.drop_table("garment_types")
    op.drop_table("service_prices")
    op.drop_table("service_options")
    op.drop_table("service_types")
    op.drop_table("customers")

    postgresql.ENUM(name="sync_operation_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="pricing_mode").drop(op.get_bind(), checkfirst=True)
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {SYNC_SEQ}"))
