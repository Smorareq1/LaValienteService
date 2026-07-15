from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.modules.identity.models import PermissionEffect


class UserRegistration(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class PermissionCreate(BaseModel):
    resource: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    action: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = Field(default=None, max_length=1000)


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    resource: str
    action: str
    description: str | None


class RoleCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    description: str | None
    is_system: bool
    created_at: datetime


class RolePermissionReplace(BaseModel):
    permission_ids: set[UUID]


class UserRoleReplace(BaseModel):
    role_ids: set[UUID]


class UserPermissionAssignment(BaseModel):
    permission_id: UUID
    effect: PermissionEffect


class UserPermissionReplace(BaseModel):
    assignments: list[UserPermissionAssignment]


class CurrentUserRead(BaseModel):
    id: UUID
    email: EmailStr
    roles: list[str]
    permissions: list[str]
