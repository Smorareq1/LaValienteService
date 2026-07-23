from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.modules.identity.models import (
    AuthSession,
    PasswordResetToken,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserPermission,
    UserRole,
)


class IdentityRepository:
    """Persistence operations for identity; contains no authorization decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _user_with_access_query() -> Select[tuple[User]]:
        return select(User).options(
            selectinload(User.role_assignments)
            .selectinload(UserRole.role)
            .selectinload(Role.permission_assignments)
            .selectinload(RolePermission.permission),
            selectinload(User.permission_assignments).selectinload(UserPermission.permission),
        )

    async def get_user_by_email(self, email: str) -> User | None:
        statement = self._user_with_access_query().where(User.email == email)
        return await self.session.scalar(statement)

    async def get_user_by_username(self, username: str) -> User | None:
        statement = self._user_with_access_query().where(User.username == username)
        return await self.session.scalar(statement)

    async def get_user_by_identifier(self, identifier: str) -> User | None:
        """Resolve a user by username or email (both stored lowercase)."""
        statement = self._user_with_access_query().where(
            or_(User.username == identifier, User.email == identifier)
        )
        return await self.session.scalar(statement)

    async def get_user_with_access(self, user_id: UUID) -> User | None:
        statement = self._user_with_access_query().where(User.id == user_id)
        return await self.session.scalar(statement)

    async def get_active_session(self, session_id: UUID, user_id: UUID) -> AuthSession | None:
        statement = select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.absolute_expires_at > datetime.now(UTC),
        )
        return await self.session.scalar(statement)

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        """Fetch a refresh token with its session, regardless of state (the service decides)."""
        statement = (
            select(RefreshToken)
            .options(selectinload(RefreshToken.session))
            .where(RefreshToken.token_hash == token_hash)
        )
        return await self.session.scalar(statement)

    async def list_active_sessions(self, user_id: UUID) -> list[AuthSession]:
        statement = (
            select(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.absolute_expires_at > datetime.now(UTC),
            )
            .order_by(AuthSession.created_at.desc())
        )
        result = await self.session.scalars(statement)
        return list(result)

    async def get_permission(self, permission_id: UUID) -> Permission | None:
        return await self.session.get(Permission, permission_id)

    async def get_permissions(self, permission_ids: set[UUID]) -> list[Permission]:
        if not permission_ids:
            return []
        result = await self.session.scalars(
            select(Permission).where(Permission.id.in_(permission_ids))
        )
        return list(result)

    async def get_role(self, role_id: UUID) -> Role | None:
        statement = (
            select(Role)
            .options(selectinload(Role.permission_assignments))
            .where(Role.id == role_id)
        )
        return await self.session.scalar(statement)

    async def get_roles(self, role_ids: set[UUID]) -> list[Role]:
        if not role_ids:
            return []
        result = await self.session.scalars(select(Role).where(Role.id.in_(role_ids)))
        return list(result)

    def add(self, entity: object) -> None:
        self.session.add(entity)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()

    async def refresh(self, entity: object) -> None:
        await self.session.refresh(entity)

    async def revoke_session(self, session: AuthSession) -> None:
        session.revoked_at = datetime.now(UTC)
        await self.commit()

    async def revoke_all_sessions(self, user_id: UUID) -> None:
        await self.session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def invalidate_reset_tokens(self, user_id: UUID) -> None:
        await self.session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
            .values(used_at=datetime.now(UTC))
        )

    async def get_active_reset_token(self, user_id: UUID) -> PasswordResetToken | None:
        statement = (
            select(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > datetime.now(UTC),
            )
            .order_by(PasswordResetToken.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def list_users(self) -> list[User]:
        statement = (
            self._user_with_access_query().order_by(User.username)
        )
        result = await self.session.scalars(statement)
        return list(result.unique())

    async def list_roles(self) -> list[Role]:
        result = await self.session.scalars(select(Role).order_by(Role.code))
        return list(result)

    async def list_permissions(self) -> list[Permission]:
        result = await self.session.scalars(
            select(Permission).order_by(Permission.resource, Permission.action)
        )
        return list(result)

    async def replace_role_permissions(self, role: Role, permissions: list[Permission]) -> None:
        role.permission_assignments = [
            RolePermission(permission=permission) for permission in permissions
        ]
        await self.commit()

    async def replace_user_roles(self, user: User, roles: list[Role]) -> None:
        user.role_assignments = [UserRole(role=role) for role in roles]
        await self.commit()

    async def replace_user_permissions(self, user: User, assignments: list[UserPermission]) -> None:
        user.permission_assignments = assignments
        await self.commit()
