from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from src.api.dependencies import CurrentUser, IdentityServiceDependency, bearer_scheme
from src.modules.identity.schemas import (
    CurrentUserRead,
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserRegistration,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=CurrentUserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserRegistration, service: IdentityServiceDependency) -> CurrentUserRead:
    user = await service.register(data)
    return service.current_user_view(user)


@router.post("/login", response_model=TokenPair)
async def login(data: LoginRequest, service: IdentityServiceDependency) -> TokenPair:
    return await service.login(data)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, service: IdentityServiceDependency) -> TokenPair:
    return await service.refresh(data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    current_user: CurrentUser,
    service: IdentityServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> Response:
    _, session_id = await service.authenticate_access_token(credentials.credentials)
    await service.logout(current_user, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=CurrentUserRead)
async def get_me(current_user: CurrentUser, service: IdentityServiceDependency) -> CurrentUserRead:
    return service.current_user_view(current_user)
