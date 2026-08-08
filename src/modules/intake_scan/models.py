from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class ScanStatus(enum.StrEnum):
    """Where a scan got to (Plan 0003 §5)."""

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class ScanJob(Base):
    """One photograph of a paper ticket, and what the model made of it.

    **Deliberately not `SyncableMixin`.** Every other table the app writes is
    mirrored on the device and travels in the change feed; a scan is not. It
    belongs to the minute it happened — a draft that a person accepts or throws
    away — and the phone that took it already holds the answer. Putting it in the
    feed would push photographs of somebody's name, phone and NIT down to every
    other device in the shop, for a row nobody reads twice.

    The direction of the foreign key is the point of D2: `scan_jobs.order_id`
    points at `orders`, never the other way round. Deleting this module on the
    day the shop drops the paper booklet is dropping one table; `orders` does not
    know it exists.
    """

    __tablename__ = "scan_jobs"
    __table_args__ = (
        Index("ix_scan_jobs_created_at", "created_at"),
        Index("ix_scan_jobs_order_id", "order_id"),
        Index("ix_scan_jobs_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    status: Mapped[ScanStatus] = mapped_column(
        Enum(ScanStatus, name="scan_status", values_callable=_enum_values),
        default=ScanStatus.PROCESSING,
    )

    #: Relative to `SCAN_STORAGE_PATH`, never the bytes (D9, and the same rule as
    #: the product image of Plan 0005 D10).
    image_path: Mapped[str] = mapped_column(String(500))

    #: Which model and which prompt produced this (D5). Without the pair, a drop
    #: in quality cannot be pinned on the change that caused it.
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(20))

    #: Exactly what came back, for audit and replay. A prompt regression is
    #: diagnosed by re-reading what the model actually said, not by guessing.
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: The draft after validation and normalization (§7) — what the app received.
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Coded discrepancies, in the shape the app already knows from the sync
    #: engine and the daily close: `total_mismatch:108.75:98.75`.
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Filled in when the draft ended up as a ticket. Nullable and `SET NULL` on
    #: delete: a scan outlives the order it produced, and nothing in `orders`
    #: may depend on this table existing.
    order_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    #: Field-by-field diff between what the model proposed and what the person
    #: actually saved (D7). This is the quality metric of §9 — the only one
    #: measured on real tickets instead of on the golden set.
    corrections: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_by_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
