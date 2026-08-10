"""create the promotions table

Plan 0001 PR 5: promotions and the foreign key that `20260802_0003` deliberately
left off `order_discounts.promotion_id`, because the table it points at did not
exist yet.

Revision ID: 20260804_0005
Revises: 20260802_0004
Create Date: 2026-08-04 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260804_0005"
down_revision: str | Sequence[str] | None = "20260802_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_SEQ = "sync_seq_global"
DISCOUNT_FK = "fk_order_discounts_promotion_id_promotions"


def upgrade() -> None:
    discount_type = postgresql.ENUM(
        "percentage",
        "fixed_amount",
        "special_price",
        name="discount_type",
        create_type=False,
    )
    discount_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "promotions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("discount_type", discount_type, nullable=False),
        sa.Column("value", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("applies_to_service_codes", sa.ARRAY(sa.String(length=50)), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_promotions"),
        sa.CheckConstraint("value > 0", name="ck_promotions_value_positive"),
        sa.CheckConstraint(
            "discount_type <> 'percentage' OR value <= 100",
            name="ck_promotions_percentage_within_100",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_promotions_window_ordered"
        ),
    )
    op.create_index("ix_promotions_code", "promotions", ["code"], unique=True)
    op.create_index("ix_promotions_valid_from", "promotions", ["valid_from"])
    op.create_index("ix_promotions_sync_seq", "promotions", ["sync_seq"])

    # The column has existed since PR 3, always null so far: until now every
    # discount was a manual one.
    op.create_foreign_key(
        DISCOUNT_FK, "order_discounts", "promotions", ["promotion_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(DISCOUNT_FK, "order_discounts", type_="foreignkey")
    op.drop_table("promotions")
    postgresql.ENUM(name="discount_type").drop(op.get_bind(), checkfirst=True)
