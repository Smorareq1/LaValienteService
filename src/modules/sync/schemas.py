from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.modules.sync.models import OperationStatus

#: Plan 0004 §11 — a push carries at most this many operations.
MAX_PUSH_OPERATIONS = 200
#: …and a pull page at most this many changes.
MAX_PULL_PAGE_SIZE = 500


class DeviceRegister(BaseModel):
    """First contact of an installation. The id comes from the device (D3)."""

    id: UUID
    name: str = Field(min_length=1, max_length=80)
    platform: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=40)


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    name: str
    platform: str | None
    app_version: str | None
    last_seen_at: datetime | None
    last_push_at: datetime | None
    last_pull_cursor: int
    revoked_at: datetime | None
    created_at: datetime


class SyncOperationIn(BaseModel):
    op_id: UUID = Field(description="Idempotency key; a fresh UUID per operation.")
    seq: int = Field(ge=0, description="Device-local order; operations apply in this order.")
    entity: str = Field(min_length=1, max_length=40)
    op_type: str = Field(min_length=1, max_length=40)
    entity_id: UUID
    #: Required on updates so the server can detect a stale write (D6).
    base_version: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    client_ts: datetime | None = None


class SyncPushRequest(BaseModel):
    device_id: UUID
    operations: list[SyncOperationIn] = Field(max_length=MAX_PUSH_OPERATIONS)


class SyncOperationResult(BaseModel):
    op_id: UUID
    status: OperationStatus
    entity_id: UUID
    server_version: int | None = None
    #: What the server actually stored, when it differs from what was sent (D10).
    server_data: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    reason: str | None = None


class SyncPushResponse(BaseModel):
    results: list[SyncOperationResult]
    #: "wipe" when the device has been revoked (D11).
    device_directive: str | None = None
    server_time: datetime


class SyncChange(BaseModel):
    entity: str
    id: UUID
    version: int
    sync_seq: int
    deleted: bool
    data: dict[str, Any]


class SyncPullResponse(BaseModel):
    changes: list[SyncChange]
    next_cursor: int
    has_more: bool
    server_time: datetime
    device_directive: str | None = None
