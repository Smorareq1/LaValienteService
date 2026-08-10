from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.modules.identity.models import PermissionEffect, ResetChannel

USERNAME_PATTERN = r"^[a-z][a-z0-9_.]{2,49}$"


class UserRegistration(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{8,15}$")
    full_name: str | None = Field(default=None, min_length=2, max_length=120)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320, description="Username or email.")
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320, description="Username or email.")
    channel: ResetChannel | None = Field(
        default=None, description="Preferred delivery channel; defaults to email, then SMS."
    )


class ResetPasswordRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=320, description="Username or email.")
    code: str = Field(pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=8, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")


class SessionRead(BaseModel):
    id: UUID
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime | None
    is_current: bool


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


class UserStatusUpdate(BaseModel):
    """Turn an account on or off.

    Deliberately its own one-field schema and not a general `UserUpdate`: the
    other columns of an account (username, password, email) each have their own
    door, and widening this one to a patch-anything endpoint would let a
    misplaced field change a credential.
    """

    is_active: bool


class CurrentUserRead(BaseModel):
    id: UUID
    username: str
    email: EmailStr | None
    full_name: str | None
    roles: list[str]
    permissions: list[str]
    #: Codes denied to this user by hand. They travel separately because a
    #: wildcard grant would otherwise re-enable them on the client.
    denied_permissions: list[str] = []


class UserSummaryRead(BaseModel):
    id: UUID
    username: str
    email: EmailStr | None
    full_name: str | None
    is_active: bool
    roles: list[str]
