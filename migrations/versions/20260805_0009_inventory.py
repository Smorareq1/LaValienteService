"""create the inventory tables

Plan 0005 PR 8 (migration #8 of §8.2): products with their image path, lots with
cost and sale price, counter sales of supplies and the kardex behind them.

The `payment_method` enum is *not* created here — it was born in the orders
migration and is reused with `create_type=False` (D13). `inventory_movement_type`
is new and is created whole.

Revision ID: 20260805_0009
Revises: 20260804_0008
Create Date: 2026-08-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260805_0009"
down_revision: str | Sequence[str] | None = "20260804_0008"
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
    movement_type = postgresql.ENUM(
        "purchase_in",
        "sale_out",
        "internal_use",
        "adjustment",
        name="inventory_movement_type",
        create_type=False,
    )
    movement_type.create(op.get_bind(), checkfirst=True)

    #: Already exists (orders migration). Declared so the columns below can point
    #: at it without CREATE TYPE running a second time.
    payment_method = postgresql.ENUM(
        "cash", "transfer", name="payment_method", create_type=False
    )

    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("unit", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_path", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("name", name="uq_products_name"),
    )
    op.create_index("ix_products_sync_seq", "products", ["sync_seq"])

    op.create_table(
        "product_lots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("lot_number", sa.Integer(), nullable=False),
        sa.Column("quantity_received", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("quantity_available", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("sale_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("received_at", sa.Date(), nullable=False),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_product_lots"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_product_lots_product_id_products",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("product_id", "lot_number", name="uq_product_lots_number"),
        sa.CheckConstraint("lot_number > 0", name="ck_product_lots_number_positive"),
        sa.CheckConstraint("quantity_received > 0", name="ck_product_lots_received_positive"),
        sa.CheckConstraint(
            "quantity_available >= 0", name="ck_product_lots_available_not_negative"
        ),
        sa.CheckConstraint(
            "unit_cost IS NULL OR unit_cost >= 0", name="ck_product_lots_cost_valid"
        ),
        sa.CheckConstraint(
            "sale_price IS NULL OR sale_price >= 0", name="ck_product_lots_price_valid"
        ),
    )
    op.create_index("ix_product_lots_product_id", "product_lots", ["product_id"])
    op.create_index("ix_product_lots_received_at", "product_lots", ["received_at"])
    op.create_index("ix_product_lots_sync_seq", "product_lots", ["sync_seq"])

    op.create_table(
        "supply_sales",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sale_date", sa.Date(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("nit", sa.String(length=20), nullable=True),
        sa.Column("method", payment_method, nullable=False),
        sa.Column("reference", sa.String(length=80), nullable=True),
        sa.Column("total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("sold_by_id", sa.Uuid(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_id", sa.Uuid(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_syncable(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_supply_sales"),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name="fk_supply_sales_customer_id_customers"
        ),
        sa.ForeignKeyConstraint(
            ["sold_by_id"], ["users.id"], name="fk_supply_sales_sold_by_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_id"], ["users.id"], name="fk_supply_sales_cancelled_by_id_users"
        ),
        sa.CheckConstraint("total >= 0", name="ck_supply_sales_total_not_negative"),
    )
    op.create_index("ix_supply_sales_sale_date", "supply_sales", ["sale_date"])
    op.create_index("ix_supply_sales_customer_id", "supply_sales", ["customer_id"])
    op.create_index("ix_supply_sales_sync_seq", "supply_sales", ["sync_seq"])

    op.create_table(
        "supply_sale_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sale_id", sa.Uuid(), nullable=False),
        sa.Column("lot_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=160), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        *_syncable(),
        *_timestamps(updated=False),
        sa.PrimaryKeyConstraint("id", name="pk_supply_sale_items"),
        sa.ForeignKeyConstraint(
            ["sale_id"],
            ["supply_sales.id"],
            name="fk_supply_sale_items_sale_id_supply_sales",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["product_lots.id"], name="fk_supply_sale_items_lot_id_product_lots"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_supply_sale_items_quantity_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_supply_sale_items_price_not_negative"),
    )
    op.create_index("ix_supply_sale_items_sale_id", "supply_sale_items", ["sale_id"])
    op.create_index("ix_supply_sale_items_lot_id", "supply_sale_items", ["lot_id"])
    op.create_index("ix_supply_sale_items_sync_seq", "supply_sale_items", ["sync_seq"])

    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lot_id", sa.Uuid(), nullable=False),
        sa.Column("movement_type", movement_type, nullable=False),
        sa.Column("quantity", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("supply_sale_item_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        *_syncable(),
        *_timestamps(updated=False),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_movements"),
        sa.ForeignKeyConstraint(
            ["lot_id"], ["product_lots.id"], name="fk_inventory_movements_lot_id_product_lots"
        ),
        sa.ForeignKeyConstraint(
            ["supply_sale_item_id"],
            ["supply_sale_items.id"],
            name="fk_inventory_movements_supply_sale_item_id_supply_sale_items",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name="fk_inventory_movements_created_by_id_users"
        ),
        sa.CheckConstraint(
            "CASE WHEN movement_type = 'adjustment' THEN quantity <> 0 ELSE quantity > 0 END",
            name="ck_inventory_movements_quantity_signed",
        ),
        sa.CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_inventory_movements_price_not_negative",
        ),
    )
    op.create_index("ix_inventory_movements_lot_id", "inventory_movements", ["lot_id"])
    op.create_index(
        "ix_inventory_movements_movement_type", "inventory_movements", ["movement_type"]
    )
    op.create_index("ix_inventory_movements_sync_seq", "inventory_movements", ["sync_seq"])


def downgrade() -> None:
    op.drop_table("inventory_movements")
    op.drop_table("supply_sale_items")
    op.drop_table("supply_sales")
    op.drop_table("product_lots")
    op.drop_table("products")
    # `payment_method` stays: orders created it and orders still uses it.
    postgresql.ENUM(name="inventory_movement_type").drop(op.get_bind(), checkfirst=True)
