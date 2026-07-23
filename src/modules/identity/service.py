from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.core.config import get_settings
from src.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from src.core.notifications import NotificationService, get_notification_service
from src.core.security import (
    create_access_token,
    create_password_reset_code,
    create_refresh_token,
    decode_access_token,
    hash_opaque_token,
    hash_password,
    verify_password,
)
from src.modules.identity.models import (
    AuthSession,
    PasswordResetToken,
    Permission,
    PermissionEffect,
    RefreshToken,
    ResetChannel,
    Role,
    User,
    UserPermission,
)
from src.modules.identity.repository import IdentityRepository
from src.modules.identity.schemas import (
    CurrentUserRead,
    ForgotPasswordRequest,
    LoginRequest,
    PermissionCreate,
    ResetPasswordRequest,
    RoleCreate,
    SessionRead,
    TokenPair,
    UserRegistration,
    UserSummaryRead,
)


class IdentityService:
    """Use cases for login, password recovery, and dynamic role-based authorization."""

    def __init__(
        self,
        repository: IdentityRepository,
        notifications: NotificationService | None = None,
    ) -> None:
        self.repository = repository
        self.notifications = notifications or get_notification_service()

    async def register(self, data: UserRegistration) -> User:
        username = data.username.lower()
        if await self.repository.get_user_by_username(username):
            raise ConflictError("An account with this username already exists.")
        email = str(data.email).lower() if data.email else None
        if email and await self.repository.get_user_by_email(email):
            raise ConflictError("An account with this email already exists.")
        user = User(
            username=username,
            email=email,
            phone=data.phone,
            full_name=data.full_name,
            password_hash=hash_password(data.password),
        )
        self.repository.add(user)
        await self.repository.commit()
        await self.repository.refresh(user)
        return user

    async def login(
        self,
        data: LoginRequest,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> TokenPair:
        user = await self.repository.get_user_by_identifier(data.identifier.strip().lower())
        if (
            user is None
            or not user.is_active
            or not verify_password(data.password, user.password_hash)
        ):
            raise AuthenticationError("Invalid username or password.")
        return await self._create_session_tokens(user, user_agent, ip_address)

    async def request_password_reset(self, data: ForgotPasswordRequest) -> None:
        """Issue a recovery code. Silent on unknown accounts to avoid enumeration."""
        user = await self.repository.get_user_by_identifier(data.identifier.strip().lower())
        if user is None or not user.is_active:
            return
        channel, destination = self._resolve_reset_channel(user, data.channel)
        if channel is None or destination is None:
            return
        code, code_hash, expires_at = create_password_reset_code()
        await self.repository.invalidate_reset_tokens(user.id)
        self.repository.add(
            PasswordResetToken(
                user_id=user.id,
                code_hash=code_hash,
                channel=channel,
                destination=destination,
                expires_at=expires_at,
            )
        )
        await self.repository.commit()
        self.notifications.send_password_reset_code(channel.value, destination, code)

    async def reset_password(self, data: ResetPasswordRequest) -> None:
        generic_error = AuthenticationError("Invalid or expired recovery code.")
        user = await self.repository.get_user_by_identifier(data.identifier.strip().lower())
        if user is None or not user.is_active:
            raise generic_error
        token = await self.repository.get_active_reset_token(user.id)
        if token is None:
            raise generic_error
        settings = get_settings()
        if token.attempts >= settings.reset_code_max_attempts:
            token.used_at = datetime.now(UTC)
            await self.repository.commit()
            raise generic_error
        if hash_opaque_token(data.code) != token.code_hash:
            token.attempts += 1
            await self.repository.commit()
            raise generic_error
        token.used_at = datetime.now(UTC)
        user.password_hash = hash_password(data.new_password)
        await self.repository.revoke_all_sessions(user.id)
        await self.repository.commit()

    @staticmethod
    def _resolve_reset_channel(
        user: User, preferred: ResetChannel | None
    ) -> tuple[ResetChannel | None, str | None]:
        if preferred is ResetChannel.SMS and user.phone:
            return ResetChannel.SMS, user.phone
        if preferred is ResetChannel.EMAIL and user.email:
            return ResetChannel.EMAIL, user.email
        if user.email:
            return ResetChannel.EMAIL, user.email
        if user.phone:
            return ResetChannel.SMS, user.phone
        return None, None

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate the refresh token inside its session, detecting stolen-token reuse."""
        invalid = AuthenticationError("Invalid or expired refresh token.")
        token = await self.repository.get_refresh_token(hash_opaque_token(refresh_token))
        if token is None:
            raise invalid
        now = datetime.now(UTC)
        session = token.session
        if session.revoked_at is not None or session.absolute_expires_at <= now:
            raise invalid
        if token.used_at is not None:
            # Un token ya rotado solo es aceptable dentro de la ventana de gracia
            # (reintento por respuesta perdida); fuera de ella se asume robo y se
            # revoca la sesión completa.
            grace = timedelta(seconds=get_settings().refresh_reuse_grace_seconds)
            if now - token.used_at > grace:
                await self.repository.revoke_session(session)
                raise invalid
        elif token.expires_at <= now:
            raise invalid
        user = await self.repository.get_user_with_access(session.user_id)
        if user is None or not user.is_active:
            raise invalid
        token.used_at = now
        return await self._rotate_session_tokens(user, session, now)

    async def logout(self, user: User, session_id: UUID) -> None:
        session = await self.repository.get_active_session(session_id, user.id)
        if session is not None:
            await self.repository.revoke_session(session)

    async def logout_all(self, user: User) -> None:
        await self.repository.revoke_all_sessions(user.id)
        await self.repository.commit()

    async def list_sessions(self, user: User, current_session_id: UUID) -> list[SessionRead]:
        sessions = await self.repository.list_active_sessions(user.id)
        return [
            SessionRead(
                id=session.id,
                user_agent=session.user_agent,
                ip_address=session.ip_address,
                created_at=session.created_at,
                last_used_at=session.last_used_at,
                is_current=session.id == current_session_id,
            )
            for session in sessions
        ]

    async def revoke_session_by_id(self, user: User, session_id: UUID) -> None:
        session = await self.repository.get_active_session(session_id, user.id)
        if session is None:
            raise NotFoundError("Session not found.")
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
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            roles=sorted(assignment.role.code for assignment in user.role_assignments),
            permissions=sorted(self.effective_permissions(user)),
        )

    async def list_users(self) -> list[UserSummaryRead]:
        users = await self.repository.list_users()
        return [
            UserSummaryRead(
                id=user.id,
                username=user.username,
                email=user.email,
                full_name=user.full_name,
                is_active=user.is_active,
                roles=sorted(assignment.role.code for assignment in user.role_assignments),
            )
            for user in users
        ]

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

    async def _create_session_tokens(
        self, user: User, user_agent: str | None, ip_address: str | None
    ) -> TokenPair:
        now = datetime.now(UTC)
        settings = get_settings()
        session = AuthSession(
            user_id=user.id,
            user_agent=user_agent[:255] if user_agent else None,
            ip_address=ip_address,
            absolute_expires_at=now + timedelta(days=settings.session_absolute_days),
            last_used_at=now,
        )
        self.repository.add(session)
        await self.repository.flush()
        return await self._issue_token_pair(user, session, now)

    async def _rotate_session_tokens(
        self, user: User, session: AuthSession, now: datetime
    ) -> TokenPair:
        session.last_used_at = now
        return await self._issue_token_pair(user, session, now)

    async def _issue_token_pair(
        self, user: User, session: AuthSession, now: datetime
    ) -> TokenPair:
        settings = get_settings()
        refresh_token, refresh_hash = create_refresh_token()
        # El refresh se desliza, pero nunca más allá del vencimiento absoluto de la sesión.
        expires_at = min(
            now + timedelta(days=settings.refresh_token_days), session.absolute_expires_at
        )
        self.repository.add(
            RefreshToken(session_id=session.id, token_hash=refresh_hash, expires_at=expires_at)
        )
        await self.repository.commit()
        access_token, expires_in = create_access_token(user.id, session.id)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
