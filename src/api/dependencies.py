from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db_session
from src.core.exceptions import AuthenticationError
from src.modules.identity.models import User
from src.modules.identity.repository import IdentityRepository
from src.modules.identity.service import IdentityService

bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_identity_service(session: DbSession) -> IdentityService:
    return IdentityService(IdentityRepository(session))


IdentityServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]


async def get_current_user(
    service: IdentityServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise AuthenticationError("Authentication credentials are required.")
    user, _ = await service.authenticate_access_token(credentials.credentials)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(permission_code: str):
    async def permission_dependency(
        user: CurrentUser,
        service: IdentityServiceDependency,
    ) -> User:
        service.ensure_permission(user, permission_code)
        return user

    return permission_dependency
