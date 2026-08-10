"""make the garment-kind uniqueness ignore tombstones

Plan 0001 PR 6, second pass. Correcting a ticket replaces its lines, and a
replaced line has to become a tombstone rather than disappear — otherwise a
device that already pulled it never learns it is gone (Plan 0004 D8). With the
constraint as it was, a ticket that kept the same garment kind collided with the
very row on its way out.

Revision ID: 20260804_0007
Revises: 20260804_0006
Create Date: 2026-08-04 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0007"
down_revision: str | Sequence[str] | None = "20260804_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "uq_order_garments_type"


def upgrade() -> None:
    op.drop_constraint(NAME, "order_garments", type_="unique")
    op.create_index(
        NAME,
        "order_garments",
        ["order_id", "garment_type_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(NAME, table_name="order_garments")
    op.create_unique_constraint(NAME, "order_garments", ["order_id", "garment_type_id"])
