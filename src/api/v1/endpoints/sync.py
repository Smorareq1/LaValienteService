from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import CurrentUser, SyncServiceDependency, require_permission
from src.modules.sync.schemas import (
    MAX_PULL_PAGE_SIZE,
    DeviceRead,
    DeviceRegister,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
)

router = APIRouter(prefix="/sync", tags=["Sync"])


@router.post("/devices", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
async def register_device(
    data: DeviceRegister, user: CurrentUser, service: SyncServiceDependency
) -> DeviceRead:
    """Register this installation. Any authenticated user may register their device."""
    device = await service.register_device(user, data)
    return DeviceRead.model_validate(device)


@router.get(
    "/devices",
    response_model=list[DeviceRead],
    dependencies=[Depends(require_permission("sync.devices.manage"))],
)
async def list_devices(service: SyncServiceDependency) -> list[DeviceRead]:
    devices = await service.list_devices()
    return [DeviceRead.model_validate(device) for device in devices]


@router.post(
    "/devices/{device_id}/revoke",
    response_model=DeviceRead,
    dependencies=[Depends(require_permission("sync.devices.manage"))],
)
async def revoke_device(device_id: UUID, service: SyncServiceDependency) -> DeviceRead:
    """Cut a lost device off. It wipes its local data on next contact (D11)."""
    device = await service.revoke_device(device_id)
    return DeviceRead.model_validate(device)


@router.post("/push", response_model=SyncPushResponse)
async def push(
    data: SyncPushRequest, user: CurrentUser, service: SyncServiceDependency
) -> SyncPushResponse:
    """Apply a batch of captured operations.

    Permissions are checked per operation against the user pushing it, so this
    endpoint itself only requires a session.
    """
    return await service.push(user, data.device_id, data.operations)


@router.get("/pull", response_model=SyncPullResponse)
async def pull(
    device_id: UUID,
    service: SyncServiceDependency,
    _user: CurrentUser,
    cursor: Annotated[int, Query(ge=0)] = 0,
    page_size: Annotated[int, Query(ge=1, le=MAX_PULL_PAGE_SIZE)] = MAX_PULL_PAGE_SIZE,
) -> SyncPullResponse:
    """Changes after `cursor`. `cursor=0` bootstraps a fresh device (§7.2)."""
    return await service.pull(device_id, cursor, page_size)
