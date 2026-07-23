from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from src.api.dependencies import (
    CurrentUser,
    IdentityServiceDependency,
    bearer_scheme,
    require_permission,
)
from src.modules.identity.schemas import (
    CurrentUserRead,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SessionRead,
    TokenPair,
    UserRegistration,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=CurrentUserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("authorization.users.manage"))],
)
async def register(data: UserRegistration, service: IdentityServiceDependency) -> CurrentUserRead:
    """Solo administradores de usuarios pueden crear cuentas nuevas."""
    user = await service.register(data)
    return service.current_user_view(user)


@router.post("/login", response_model=TokenPair)
async def login(
    request: Request, data: LoginRequest, service: IdentityServiceDependency
) -> TokenPair:
    return await service.login(
        data,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, service: IdentityServiceDependency) -> TokenPair:
    return await service.refresh(data.refresh_token)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    data: ForgotPasswordRequest, service: IdentityServiceDependency
) -> dict[str, str]:
    """Always responds 202 so account existence cannot be probed."""
    await service.request_password_reset(data)
    return {"detail": "If the account exists, a recovery code was sent."}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    data: ResetPasswordRequest, service: IdentityServiceDependency
) -> Response:
    await service.reset_password(data)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    current_user: CurrentUser,
    service: IdentityServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> Response:
    _, session_id = await service.authenticate_access_token(credentials.credentials)
    await service.logout(current_user, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(current_user: CurrentUser, service: IdentityServiceDependency) -> Response:
    """Revoca todas las sesiones del usuario (todos los dispositivos)."""
    await service.logout_all(current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    current_user: CurrentUser,
    service: IdentityServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> list[SessionRead]:
    _, session_id = await service.authenticate_access_token(credentials.credentials)
    return await service.list_sessions(current_user, session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: UUID,
    current_user: CurrentUser,
    service: IdentityServiceDependency,
) -> Response:
    await service.revoke_session_by_id(current_user, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=CurrentUserRead)
async def get_me(current_user: CurrentUser, service: IdentityServiceDependency) -> CurrentUserRead:
    return service.current_user_view(current_user)
