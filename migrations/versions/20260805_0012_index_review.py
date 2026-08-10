"""index review for the close of phase 1

Plan 0005 PR 12, "revisión de índices". Three things, in one pass over the schema
the six previous migrations left behind:

**Duplicated uniqueness.** `users.username`, `users.email`, `service_types.code`
and `sync_operations.op_id` each ended up with two btrees: a unique constraint
and a plain index on the same column. The second can never answer anything the
first does not, and both are maintained on every write. Each pair collapses into
the single unique index the models ask for — which is also the whole of the drift
`alembic check` had been reporting since PR 1.

**Indexes a composite already covers.** `attendance_records.employee_id`,
`product_lots.product_id` and `service_options.service_type_id` are the leading
column of a composite index that exists on the same table, so the planner has no
reason to pick them. Same argument, and same fix, as migration 0006 for
`orders.order_date`.

**Two range scans with nothing behind them.** The daily close counts tickets by
`orders.delivered_at` and money by `order_payments.paid_at` (D1), and the preview
is live on screen all day, so both ran as full scans on tables that only grow.
The kardex adds a third: `inventory_movements` is the table that gains a row on
every sale and every adjustment, is never cut by date, and is listed newest
first.

Revision ID: 20260805_0012
Revises: 20260805_0011
Create Date: 2026-08-05 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260805_0012"
down_revision: str | Sequence[str] | None = "20260805_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column, plain index name, unique constraint name)
DUPLICATED_UNIQUE = (
    ("users", "username", "ix_users_username", "uq_users_username"),
    ("users", "email", "ix_users_email", "uq_users_email"),
    ("service_types", "code", "ix_service_types_code", "uq_service_types_code"),
    ("sync_operations", "op_id", "ix_sync_operations_op_id", "uq_sync_operations_op_id"),
)

#: (index name, table, column) — each one the prefix of a composite on the table.
COVERED_BY_A_COMPOSITE = (
    ("ix_attendance_records_employee_id", "attendance_records", "employee_id"),
    ("ix_product_lots_product_id", "product_lots", "product_id"),
    ("ix_service_options_service_type_id", "service_options", "service_type_id"),
)

#: (index name, table, column) — ranges and orderings that had no index.
NEW = (
    ("ix_orders_delivered_at", "orders", "delivered_at"),
    ("ix_order_payments_paid_at", "order_payments", "paid_at"),
    ("ix_inventory_movements_created_at", "inventory_movements", "created_at"),
)


def upgrade() -> None:
    for table, column, index_name, constraint_name in DUPLICATED_UNIQUE:
        op.drop_index(index_name, table_name=table)
        op.drop_constraint(constraint_name, table, type_="unique")
        op.create_index(index_name, table, [column], unique=True)

    for index_name, table, _column in COVERED_BY_A_COMPOSITE:
        op.drop_index(index_name, table_name=table)

    for index_name, table, column in NEW:
        op.create_index(index_name, table, [column])


def downgrade() -> None:
    for index_name, table, _column in NEW:
        op.drop_index(index_name, table_name=table)

    for index_name, table, column in COVERED_BY_A_COMPOSITE:
        op.create_index(index_name, table, [column])

    for table, column, index_name, constraint_name in DUPLICATED_UNIQUE:
        op.drop_index(index_name, table_name=table)
        op.create_unique_constraint(constraint_name, table, [column])
        op.create_index(index_name, table, [column])
