from uuid import UUID

from fastapi import APIRouter, Depends, status

from src.api.dependencies import IdentityServiceDependency, require_permission
from src.modules.identity.schemas import (
    PermissionCreate,
    PermissionRead,
    RoleCreate,
    RolePermissionReplace,
    RoleRead,
    UserPermissionReplace,
    UserRoleReplace,
)

router = APIRouter(prefix="/authorization", tags=["Authorization"])


@router.post(
    "/permissions",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("authorization.permissions.manage"))],
)
async def create_permission(
    data: PermissionCreate, service: IdentityServiceDependency
) -> PermissionRead:
    permission = await service.create_permission(data)
    return PermissionRead.model_validate(permission)


@router.post(
    "/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("authorization.roles.manage"))],
)
async def create_role(data: RoleCreate, service: IdentityServiceDependency) -> RoleRead:
    role = await service.create_role(data)
    return RoleRead.model_validate(role)


@router.put(
    "/roles/{role_id}/permissions",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("authorization.roles.manage"))],
)
async def replace_role_permissions(
    role_id: UUID, data: RolePermissionReplace, service: IdentityServiceDependency
) -> None:
    await service.replace_role_permissions(role_id, data.permission_ids)


@router.put(
    "/users/{user_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("authorization.users.manage"))],
)
async def replace_user_roles(
    user_id: UUID, data: UserRoleReplace, service: IdentityServiceDependency
) -> None:
    await service.replace_user_roles(user_id, data.role_ids)


@router.put(
    "/users/{user_id}/permissions",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("authorization.users.manage"))],
)
async def replace_user_permissions(
    user_id: UUID, data: UserPermissionReplace, service: IdentityServiceDependency
) -> None:
    assignments = [(assignment.permission_id, assignment.effect) for assignment in data.assignments]
    await service.replace_user_permissions(user_id, assignments)
