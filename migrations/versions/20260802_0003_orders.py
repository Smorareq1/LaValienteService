"""create the orders tables

Plan 0001 PR 3: the ticket itself — header, garment detail, charge lines with
their prices frozen, and discounts. The life cycle beyond `received` (delivery,
cancellation, payments) is PR 4; the `order_status` enum is created whole anyway
because adding a value to an enum the devices already mirror is a migration on
live data.

Revision ID: 20260802_0003
Revises: 20260731_0002
Create Date: 2026-08-02 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0003"
down_revision: str | Sequence[str] | None = "20260731_0002"
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


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    order_status = postgresql.ENUM(
        "received",
        "in_progress",
        "ready",
        "delivered",
        "cancelled",
        name="order_status",
        create_type=False,
    )
    order_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("daily_number", sa.Integer(), nullable=False),
        sa.Column("booklet_serial", sa.String(length=20), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("nit", sa.String(length=20), nullable=True),
        sa.Column("weight_lbs", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("total_pieces", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("observations", sa.Text(), nullable=True),
        sa.Column("status", order_status, nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("discount_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("received_by_id", sa.Uuid(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_by_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_id", sa.Uuid(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_syncable_columns(),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name="fk_orders_customer_id_customers"
        ),
        sa.ForeignKeyConstraint(
            ["received_by_id"], ["users.id"], name="fk_orders_received_by_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["delivered_by_id"], ["users.id"], name="fk_orders_delivered_by_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_id"], ["users.id"], name="fk_orders_cancelled_by_id_users"
        ),
        # The number people say out loud ("el 4 de hoy") cannot belong to two
        # tickets (D4, D11).
        sa.UniqueConstraint("order_date", "daily_number", name="uq_orders_daily_number"),
        sa.CheckConstraint("daily_number > 0", name="ck_orders_daily_number_positive"),
        sa.CheckConstraint("total = subtotal - discount_total", name="ck_orders_total_matches"),
        sa.CheckConstraint("discount_total <= subtotal", name="ck_orders_discount_within"),
    )
    op.create_index("ix_orders_order_date", "orders", ["order_date"])
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_sync_seq", "orders", ["sync_seq"])
    # Partial, because the printed booklet is optional and every ticket captured
    # without one would otherwise collide with the rest.
    op.create_index(
        "uq_orders_booklet_serial",
        "orders",
        ["booklet_serial"],
        unique=True,
        postgresql_where=sa.text("booklet_serial IS NOT NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "order_garments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("garment_type_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_delivered", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_syncable_columns(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_order_garments"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_garments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["garment_type_id"],
            ["garment_types.id"],
            name="fk_order_garments_garment_type_id_garment_types",
        ),
        sa.UniqueConstraint("order_id", "garment_type_id", name="uq_order_garments_type"),
        sa.CheckConstraint("quantity > 0", name="ck_order_garments_quantity_positive"),
    )
    op.create_index("ix_order_garments_order_id", "order_garments", ["order_id"])
    op.create_index("ix_order_garments_sync_seq", "order_garments", ["sync_seq"])

    op.create_table(
        "order_charges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("service_type_id", sa.Uuid(), nullable=False),
        sa.Column("service_option_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=160), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        *_syncable_columns(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_order_charges"),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_charges_order_id_orders", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_type_id"],
            ["service_types.id"],
            name="fk_order_charges_service_type_id_service_types",
        ),
        sa.ForeignKeyConstraint(
            ["service_option_id"],
            ["service_options.id"],
            name="fk_order_charges_service_option_id_service_options",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_order_charges_quantity_positive"),
    )
    op.create_index("ix_order_charges_order_id", "order_charges", ["order_id"])
    op.create_index("ix_order_charges_sync_seq", "order_charges", ["sync_seq"])

    op.create_table(
        "order_discounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        # No foreign key yet: `promotions` is PR 5, and it adds the constraint
        # over this column when the table exists.
        sa.Column("promotion_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=160), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        *_syncable_columns(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_order_discounts"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_discounts_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("amount > 0", name="ck_order_discounts_amount_positive"),
    )
    op.create_index("ix_order_discounts_order_id", "order_discounts", ["order_id"])
    op.create_index("ix_order_discounts_sync_seq", "order_discounts", ["sync_seq"])


def downgrade() -> None:
    op.drop_table("order_discounts")
    op.drop_table("order_charges")
    op.drop_table("order_garments")
    op.drop_table("orders")
    postgresql.ENUM(name="order_status").drop(op.get_bind(), checkfirst=True)
