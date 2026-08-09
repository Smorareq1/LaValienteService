from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db_session
from src.core.exceptions import AuthenticationError
from src.modules.catalog.repository import CatalogRepository
from src.modules.catalog.service import CatalogService
from src.modules.customers.repository import CustomersRepository
from src.modules.customers.service import CustomersService
from src.modules.daily_close.repository import DailyCloseRepository
from src.modules.daily_close.service import DailyCloseService
from src.modules.expenses.repository import ExpensesRepository
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.identity.permissions import ensure_catalogued
from src.modules.identity.repository import IdentityRepository
from src.modules.identity.service import IdentityService
from src.modules.intake_scan.extractor import GeminiExtractor
from src.modules.intake_scan.repository import ScanRepository
from src.modules.intake_scan.service import ScanService
from src.modules.inventory.repository import InventoryRepository
from src.modules.inventory.service import InventoryService
from src.modules.orders.repository import OrdersRepository
from src.modules.orders.service import OrdersService
from src.modules.promotions.repository import PromotionsRepository
from src.modules.promotions.service import PromotionsService
from src.modules.staff.repository import StaffRepository
from src.modules.staff.service import StaffService
from src.modules.sync.repository import SyncRepository
from src.modules.sync.service import SyncService

bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]

#: Every gate `require_permission` has built, by the dependency it returned.
#: Filled while the routers are imported. `scripts.dump_api_reference` reads it to
#: write the route table of `docs/API.md` from the running app rather than from a
#: list somebody remembered to update.
PERMISSION_BY_DEPENDENCY: dict[Callable[..., Awaitable[User]], str] = {}

#: Optimistic concurrency on an edit (Plan 0004 D6). Shared because the answer to
#: "what happens if I omit it" has to be the same on every screen: the write goes
#: through. A device always sends one; the admin screens may not.
BaseVersion = Annotated[
    int | None,
    Query(ge=1, description="Version the edit was built on; omit to force the write."),
]


def get_identity_service(session: DbSession) -> IdentityService:
    return IdentityService(IdentityRepository(session))


IdentityServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]


def get_catalog_service(session: DbSession) -> CatalogService:
    return CatalogService(CatalogRepository(session))


CatalogServiceDependency = Annotated[CatalogService, Depends(get_catalog_service)]


def get_customers_service(session: DbSession) -> CustomersService:
    return CustomersService(CustomersRepository(session))


CustomersServiceDependency = Annotated[CustomersService, Depends(get_customers_service)]


def get_expenses_service(session: DbSession) -> ExpensesService:
    """Expenses check the employee, the working day and the lot they point at,
    so the two repositories that own those rows come along."""
    return ExpensesService(
        ExpensesRepository(session),
        StaffRepository(session),
        InventoryRepository(session),
        DailyCloseRepository(session),
    )


ExpensesServiceDependency = Annotated[ExpensesService, Depends(get_expenses_service)]


def get_inventory_service(session: DbSession) -> InventoryService:
    """A counter sale may name a customer, and a lot arriving may be paid for.

    One session across the three, which is what makes registering a lot with its
    expense a single transaction (§6.3) instead of two writes that can disagree.
    """
    return InventoryService(
        InventoryRepository(session),
        CustomersService(CustomersRepository(session)),
        get_expenses_service(session),
        DailyCloseRepository(session),
    )


InventoryServiceDependency = Annotated[InventoryService, Depends(get_inventory_service)]


def get_orders_service(session: DbSession) -> OrdersService:
    """Taking an order touches three modules, so it composes their services.

    One session across all of them is the point: the customer registered at the
    counter and the ticket that needed them are a single transaction.
    """
    return OrdersService(
        OrdersRepository(session),
        CatalogRepository(session),
        PromotionsRepository(session),
        CustomersService(CustomersRepository(session)),
        IdentityService(IdentityRepository(session)),
        DailyCloseRepository(session),
    )


OrdersServiceDependency = Annotated[OrdersService, Depends(get_orders_service)]


def get_daily_close_service(session: DbSession) -> DailyCloseService:
    """The close adds up the three modules that hold money, so it composes them.

    The same session throughout, which is what makes the acta a photograph: the
    payments, the sales and the expenses it adds up are read inside one
    transaction, not three that a counter sale could slip between.

    Note the direction. These three take a `DailyCloseRepository` to ask whether
    a day is closed, and this one takes their services to ask what the day was
    worth — the cycle is avoided because they hold the `ClosedDays` protocol of
    `daily_close/lock.py` and never the service.
    """
    return DailyCloseService(
        DailyCloseRepository(session),
        get_orders_service(session),
        get_inventory_service(session),
        get_expenses_service(session),
    )


DailyCloseServiceDependency = Annotated[DailyCloseService, Depends(get_daily_close_service)]


def get_promotions_service(session: DbSession) -> PromotionsService:
    """Administering a promotion checks the services it names, so it needs the
    catalog alongside its own repository."""
    return PromotionsService(PromotionsRepository(session), CatalogRepository(session))


PromotionsServiceDependency = Annotated[PromotionsService, Depends(get_promotions_service)]


def get_scan_service(session: DbSession) -> ScanService:
    """Reading a ticket needs the garment catalog to map names and the customers
    to suggest one; finding one already captured needs the orders. All three
    read-only — this module proposes, it never writes a ticket (D2).

    The extractor is built here and nowhere else, which is what D6 buys: swapping
    provider is this line. Kept out of the service's constructor default so a
    test can hand over a fake without the real one ever being constructed.
    """
    return ScanService(
        ScanRepository(session),
        GeminiExtractor(),
        CatalogRepository(session),
        CustomersRepository(session),
        OrdersRepository(session),
    )


ScanServiceDependency = Annotated[ScanService, Depends(get_scan_service)]


def get_staff_service(session: DbSession) -> StaffService:
    return StaffService(StaffRepository(session))


StaffServiceDependency = Annotated[StaffService, Depends(get_staff_service)]


def get_sync_service(session: DbSession) -> SyncService:
    """Sync reuses the domain services rather than reaching into repositories.

    That is Plan 0004 D2 made structural: an operation arriving from a device runs
    the same code path an HTTP request would.

    Every module that accepts an operation is composed here, and each one already
    holds a `DailyCloseRepository`, so the day lock of Plan 0005 §6.4 applies to a
    push without this transport knowing the rule exists.
    """
    return SyncService(
        SyncRepository(session),
        CustomersService(CustomersRepository(session)),
        get_orders_service(session),
        IdentityService(IdentityRepository(session)),
        get_staff_service(session),
        get_expenses_service(session),
        get_inventory_service(session),
        # The app captures locally and pushes, so this — not `POST /orders` — is
        # the path a scanned ticket actually takes. Without it the corrections
        # metric of Plan 0003 D7 would only ever see tickets nobody scanned.
        get_scan_service(session),
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
    """Gate a route behind a permission the catalog defines.

    The catalog check happens here, while the routers are being imported, so a
    misspelled code brings the process down at startup. Left to runtime it would
    be the quietest bug in the system: administrators hold the wildcard and pass
    anything, so the route would work for whoever tested it and deny every
    collaborator, forever, with a plain 403.
    """
    ensure_catalogued(permission_code)

    async def permission_dependency(
        user: CurrentUser,
        service: IdentityServiceDependency,
    ) -> User:
        service.ensure_permission(user, permission_code)
        return user

    PERMISSION_BY_DEPENDENCY[permission_dependency] = permission_code
    return permission_dependency
