"""read the «Registro Diario» sheet, and record that it was imported

A third document joins Plan 0003: the daily sheet of Plan 0005 §1, photographed
at the end of the day so its rows stop being typed one by one. It reads through
the same table, the same provider and the same budget as a ticket — and it is the
only purpose that ends in the server writing money, which is what the two columns
below are for.

`applied_at` is the guard against importing the same sheet twice. The ids of the
payments and expenses an import creates are derived from the scan (uuid5), so a
retried request collides with rows that already exist and is recognised; a second
*deliberate* import is a different mistake, and this is what lets it be refused by
name instead of collecting the day's money again.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on PostgreSQL
below 12, and even above it it cannot be used in the same transaction that then
*writes* the new value. Alembic runs migrations in a transaction, so the value is
added with an autocommit block of its own before the columns are touched.

Revision ID: 20260809_0015
Revises: 20260809_0014
Create Date: 2026-08-09 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_0015"
down_revision: str | Sequence[str] | None = "20260809_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE scan_purpose ADD VALUE IF NOT EXISTS 'cash_close'")

    op.add_column(
        "scan_jobs",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scan_jobs",
        sa.Column("applied_result", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scan_jobs", "applied_result")
    op.drop_column("scan_jobs", "applied_at")
    # The enum value stays. Removing a value from a Postgres enum means recreating
    # the type and rewriting every column that uses it, and a `cash_close` row
    # that already exists would have nowhere to go. An unused value costs nothing.
