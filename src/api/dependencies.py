from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db_session
from src.core.exceptions import AuthenticationError
from src.modules.catalog.repository import CatalogRepository
from src.modules.catalog.service import CatalogService
from src.modules.customers.repository import CustomersRepository
from src.modules.customers.service import CustomersService
from src.modules.identity.models import User
from src.modules.identity.repository import IdentityRepository
from src.modules.identity.service import IdentityService
from src.modules.orders.repository import OrdersRepository
from src.modules.orders.service import OrdersService
from src.modules.sync.repository import SyncRepository
from src.modules.sync.service import SyncService

bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_identity_service(session: DbSession) -> IdentityService:
    return IdentityService(IdentityRepository(session))


IdentityServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]


def get_catalog_service(session: DbSession) -> CatalogService:
    return CatalogService(CatalogRepository(session))


CatalogServiceDependency = Annotated[CatalogService, Depends(get_catalog_service)]


def get_customers_service(session: DbSession) -> CustomersService:
    return CustomersService(CustomersRepository(session))


CustomersServiceDependency = Annotated[CustomersService, Depends(get_customers_service)]


def get_orders_service(session: DbSession) -> OrdersService:
    """Taking an order touches three modules, so it composes their services.

    One session across all of them is the point: the customer registered at the
    counter and the ticket that needed them are a single transaction.
    """
    return OrdersService(
        OrdersRepository(session),
        CatalogRepository(session),
        CustomersService(CustomersRepository(session)),
        IdentityService(IdentityRepository(session)),
    )


OrdersServiceDependency = Annotated[OrdersService, Depends(get_orders_service)]


def get_sync_service(session: DbSession) -> SyncService:
    """Sync reuses the domain services rather than reaching into repositories.

    That is Plan 0004 D2 made structural: an operation arriving from a device runs
    the same code path an HTTP request would.
    """
    return SyncService(
        SyncRepository(session),
        CustomersService(CustomersRepository(session)),
        get_orders_service(session),
        IdentityService(IdentityRepository(session)),
    )


SyncServiceDependency = Annotated[SyncService, Depends(get_sync_service)]


async def get_current_user(
    service: IdentityServiceDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise AuthenticationError("Authentication credentials are required.")
    user, _ = await service.authenticate_access_token(credentials.credentials)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(permission_code: str) -> Callable[..., Awaitable[User]]:
    async def permission_dependency(
        user: CurrentUser,
        service: IdentityServiceDependency,
    ) -> User:
        service.ensure_permission(user, permission_code)
        return user

    return permission_dependency
