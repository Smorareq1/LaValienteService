"""drop the index on orders.order_date, which another one already covers

Plan 0001 PR 6, "revisión de índices". Every query that filters by business date
— the day's list, the daily summary, the next correlative — is already served by
`uq_orders_daily_number (order_date, daily_number)`, whose leading column is
exactly `order_date`. The standalone index answered nothing extra and cost a
write on every ticket taken.

Revision ID: 20260804_0006
Revises: 20260804_0005
Create Date: 2026-08-04 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0006"
down_revision: str | Sequence[str] | None = "20260804_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_orders_order_date"


def upgrade() -> None:
    op.drop_index(INDEX, table_name="orders")


def downgrade() -> None:
    op.create_index(INDEX, "orders", ["order_date"])
