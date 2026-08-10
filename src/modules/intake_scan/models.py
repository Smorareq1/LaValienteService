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


class ScanPurpose(enum.StrEnum):
    """What the photograph was taken *for*.

    Both purposes are the same call to the same provider with the same prompt,
    and they share the daily budget of §8 — but they are not the same event, and
    a column is what keeps them from being averaged together.

    `intake` is §4: a ticket being captured, which ends in a draft a person
    corrects, and whose corrections are the quality metric of §9. `lookup` is a
    ticket that already exists being *found* so it can be handed back; nobody
    corrects anything, so it proposes nothing and produces no diff. Scoring the
    model on rows that never had a draft to get wrong would drag the metric
    toward whatever fraction of the day's photos happened to be deliveries.

    `cash_close` is a third document altogether — the «Registro Diario» sheet of
    Plan 0005 §1, a whole day's money on one page. It reads with its own prompt
    and its own schema, and it is the only purpose that ends in the server
    *writing*: payments, deliveries, expenses and attendance, all of them after a
    person has confirmed the row. That is why it has `applied_at` below and the
    other two do not.
    """

    INTAKE = "intake"
    LOOKUP = "lookup"
    CASH_CLOSE = "cash_close"


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
    #: Capture or delivery lookup. Defaulted to `intake` so every row written
    #: before this column existed keeps meaning what it meant.
    #:
    #: Without an index of its own: two values over a table that grows by a few
    #: dozen rows a day is exactly what 0012 went through the schema removing.
    purpose: Mapped[ScanPurpose] = mapped_column(
        Enum(ScanPurpose, name="scan_purpose", values_callable=_enum_values),
        default=ScanPurpose.INTAKE,
        server_default=ScanPurpose.INTAKE.value,
    )

    #: Relative to `SCAN_STORAGE_PATH`, never the bytes (D9, and the same rule as
    #: the product image of Plan 0005 D10).
    #:
    #: Empty for a `lookup`: that photo is a ticket already in the database being
    #: pointed at, so keeping it would add a picture of somebody's name, phone and
    #: NIT to the disk for an answer nobody reviews side by side. `list_expired`
    #: already reads `image_path != ""` as "there is no file here".
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

    #: When a `cash_close` sheet was imported, and what the import did.
    #:
    #: The pair is the idempotency guard of the only purpose that writes. The ids
    #: of the payments and expenses it creates are derived from this scan, so a
    #: retried request lands on rows that already exist and is recognised rather
    #: than duplicated — but a *second deliberate* import of the same sheet is a
    #: different mistake, and this is what lets the endpoint refuse it by name
    #: instead of quietly collecting the day's money twice.
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_by_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
