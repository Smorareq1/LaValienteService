"""tell a capture scan from a delivery lookup

Plan 0003 gains a second use of the same reading: a photo of a ticket that
already exists, taken to *find* it at the counter so it can be handed back
(Plan 0006 §7.1.1). Same provider, same prompt, same daily budget — but not the
same event, and the quality metric of §9 is only meaningful over the ones that
had a draft somebody could correct.

Defaulted to `intake` rather than made nullable: every row that exists today was
a capture, and a null would make the metric query decide what to do about rows
whose purpose is not in doubt.

Revision ID: 20260809_0014
Revises: 20260808_0013
Create Date: 2026-08-09 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_0014"
down_revision: str | Sequence[str] | None = "20260808_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same shape as 0013: `create_type` belongs to the Postgres dialect, so the
# generic `sa.Enum` would emit a second CREATE TYPE when the column is added.
SCAN_PURPOSE = postgresql.ENUM("intake", "lookup", name="scan_purpose", create_type=False)


def upgrade() -> None:
    SCAN_PURPOSE.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "scan_jobs",
        sa.Column(
            "purpose",
            SCAN_PURPOSE,
            nullable=False,
            server_default="intake",
        ),
    )


def downgrade() -> None:
    op.drop_column("scan_jobs", "purpose")
    postgresql.ENUM(name="scan_purpose").drop(op.get_bind(), checkfirst=True)
