from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from src.modules.identity.models import (
    AuthSession,
    Permission,
    PermissionEffect,
    Role,
    User,
    UserPermission,
)
from src.modules.identity.repository import IdentityRepository
from src.modules.identity.schemas import (
    CurrentUserRead,
    LoginRequest,
    PermissionCreate,
    RoleCreate,
    TokenPair,
    UserRegistration,
)


class IdentityService:
    """Use cases for login and dynamic role-based authorization."""

    def __init__(self, repository: IdentityRepository) -> None:
        self.repository = repository

    async def register(self, data: UserRegistration) -> User:
        email = str(data.email).lower()
        if await self.repository.get_user_by_email(email):
            raise ConflictError("An account with this email already exists.")
        user = User(email=email, password_hash=hash_password(data.password))
        self.repository.add(user)
        await self.repository.commit()
        await self.repository.refresh(user)
        return user

    async def login(self, data: LoginRequest) -> TokenPair:
        user = await self.repository.get_user_by_email(str(data.email).lower())
        if (
            user is None
            or not user.is_active
            or not verify_password(data.password, user.password_hash)
        ):
            raise AuthenticationError("Invalid email or password.")
        return await self._create_session_tokens(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        session = await self.repository.get_active_session_by_refresh_hash(
            hash_opaque_token(refresh_token)
        )
        if session is None:
            raise AuthenticationError("Invalid or expired refresh token.")
        user = await self.repository.get_user_with_access(session.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("The account is not active.")
        await self.repository.revoke_session(session)
        return await self._create_session_tokens(user)

    async def logout(self, user: User, session_id: UUID) -> None:
        session = await self.repository.get_active_session(session_id, user.id)
        if session is not None:
            await self.repository.revoke_session(session)

    async def authenticate_access_token(self, token: str) -> tuple[User, UUID]:
        user_id, session_id = decode_access_token(token)
        session = await self.repository.get_active_session(session_id, user_id)
        user = await self.repository.get_user_with_access(user_id)
        if session is None or user is None or not user.is_active:
            raise AuthenticationError("Session is invalid or revoked.")
        return user, session_id

    @staticmethod
    def effective_permissions(user: User) -> set[str]:
        role_permissions = {
            assignment.permission.code
            for user_role in user.role_assignments
            for assignment in user_role.role.permission_assignments
        }
        grants = {
            assignment.permission.code
            for assignment in user.permission_assignments
            if assignment.effect is PermissionEffect.GRANT
        }
        denials = {
            assignment.permission.code
            for assignment in user.permission_assignments
            if assignment.effect is PermissionEffect.DENY
        }
        return (role_permissions | grants) - denials

    def current_user_view(self, user: User) -> CurrentUserRead:
        return CurrentUserRead(
            id=user.id,
            email=user.email,
            roles=sorted(assignment.role.code for assignment in user.role_assignments),
            permissions=sorted(self.effective_permissions(user)),
        )

    def ensure_permission(self, user: User, permission_code: str) -> None:
        if permission_code not in self.effective_permissions(user):
            raise AuthorizationError("The account lacks the required permission.")

    async def create_permission(self, data: PermissionCreate) -> Permission:
        permission = Permission(
            resource=data.resource,
            action=data.action,
            description=data.description,
        )
        self.repository.add(permission)
        try:
            await self.repository.commit()
        except IntegrityError as error:
            await self.repository.session.rollback()
            raise ConflictError("This permission already exists.") from error
        await self.repository.refresh(permission)
        return permission

    async def create_role(self, data: RoleCreate) -> Role:
        role = Role(code=data.code, name=data.name, description=data.description)
        self.repository.add(role)
        try:
            await self.repository.commit()
        except IntegrityError as error:
            await self.repository.session.rollback()
            raise ConflictError("A role with this code or name already exists.") from error
        await self.repository.refresh(role)
        return role

    async def replace_role_permissions(self, role_id: UUID, permission_ids: set[UUID]) -> None:
        role = await self.repository.get_role(role_id)
        if role is None:
            raise NotFoundError("Role not found.")
        permissions = await self.repository.get_permissions(permission_ids)
        if len(permissions) != len(permission_ids):
            raise NotFoundError("One or more permissions do not exist.")
        await self.repository.replace_role_permissions(role, permissions)

    async def replace_user_roles(self, user_id: UUID, role_ids: set[UUID]) -> None:
        user = await self.repository.get_user_with_access(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        roles = await self.repository.get_roles(role_ids)
        if len(roles) != len(role_ids):
            raise NotFoundError("One or more roles do not exist.")
        await self.repository.replace_user_roles(user, roles)

    async def replace_user_permissions(
        self, user_id: UUID, assignments: list[tuple[UUID, PermissionEffect]]
    ) -> None:
        user = await self.repository.get_user_with_access(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        permission_ids = {permission_id for permission_id, _ in assignments}
        if len(permission_ids) != len(assignments):
            raise ConflictError("A permission may only be assigned once per user.")
        permissions = await self.repository.get_permissions(permission_ids)
        permission_by_id = {permission.id: permission for permission in permissions}
        if len(permission_by_id) != len(permission_ids):
            raise NotFoundError("One or more permissions do not exist.")
        rows = [
            UserPermission(permission=permission_by_id[permission_id], effect=effect)
            for permission_id, effect in assignments
        ]
        await self.repository.replace_user_permissions(user, rows)

    async def _create_session_tokens(self, user: User) -> TokenPair:
        refresh_token, refresh_hash, expires_at = create_refresh_token()
        session = AuthSession(
            user_id=user.id, refresh_token_hash=refresh_hash, expires_at=expires_at
        )
        self.repository.add(session)
        await self.repository.commit()
        await self.repository.refresh(session)
        return TokenPair(
            access_token=create_access_token(user.id, session.id),
            refresh_token=refresh_token,
        )
