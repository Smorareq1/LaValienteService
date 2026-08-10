from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.modules.identity.models import User


class OperationStatus(enum.StrEnum):
    """Outcome of a synchronization operation (Plan 0004 §6.2)."""

    #: Applied now.
    APPLIED = "applied"
    #: Seen before; the stored result was replayed (D4).
    ALREADY_APPLIED = "already_applied"
    #: A domain rule said no. Goes to the device's review queue.
    REJECTED = "rejected"
    #: The device built it on a version the server has since moved past (D6).
    CONFLICT = "conflict"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class SyncDevice(Base):
    """One installation of the app. Ids are generated device-side at first login."""

    __tablename__ = "sync_devices"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pull_cursor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: Set by an admin on loss or theft; the device is wiped on next contact (D11).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class SyncOperation(Base):
    """Idempotency log: every operation is applied exactly once (D4).

    The row is both the dedup key and the receipt — a replay after a mid-sync
    crash re-serves `result` instead of creating a second order or payment.
    """

    __tablename__ = "sync_operations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    op_id: Mapped[UUID] = mapped_column(Uuid, unique=True, index=True)
    device_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_devices.id", ondelete="CASCADE"), index=True
    )
    #: The operation runs with the permissions of whoever captured it, which may
    #: no longer be who is holding the device.
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity: Mapped[str] = mapped_column(String(40))
    op_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus, name="sync_operation_status", values_callable=_enum_values)
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Device clock at capture; compared against server time to spot bad clocks.
    client_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device: Mapped[SyncDevice] = relationship()
