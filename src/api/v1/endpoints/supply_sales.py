from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import CurrentUser, InventoryServiceDependency, require_permission
from src.modules.inventory.schemas import (
    SupplySaleCancel,
    SupplySaleCreate,
    SupplySalePage,
    SupplySaleRead,
)

#: A document of its own and not a sub-resource of inventory (D2): what it sells
#: happens to be stock, but what it *is* is an income of the day (§6.1).
router = APIRouter(prefix="/supply-sales", tags=["Supply sales"])


@router.post(
    "",
    response_model=SupplySaleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("supply_sales.create"))],
)
async def create_sale(
    data: SupplySaleCreate, service: InventoryServiceDependency, user: CurrentUser
) -> SupplySaleRead:
    """Sell supplies over the counter.

    The body carries products and quantities, never lots and never prices: the
    server assigns lots FIFO and takes each price from the lot it came out of
    (D5). Sending an `id` makes the sale idempotent, which is what a device
    captured offline needs.
    """
    sale = await service.create_sale(data, actor=user)
    return SupplySaleRead.model_validate(sale)


@router.get(
    "",
    response_model=SupplySalePage,
    dependencies=[Depends(require_permission("supply_sales.read"))],
)
async def search_sales(
    service: InventoryServiceDependency,
    sale_date: Annotated[date | None, Query(alias="date")] = None,
    include_cancelled: bool = True,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SupplySalePage:
    return await service.search_sales(
        sale_date=sale_date,
        include_cancelled=include_cancelled,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{sale_id}",
    response_model=SupplySaleRead,
    dependencies=[Depends(require_permission("supply_sales.read"))],
)
async def get_sale(sale_id: UUID, service: InventoryServiceDependency) -> SupplySaleRead:
    sale = await service.get_sale(sale_id)
    return SupplySaleRead.model_validate(sale)


@router.post(
    "/{sale_id}/cancel",
    response_model=SupplySaleRead,
    dependencies=[Depends(require_permission("supply_sales.cancel"))],
)
async def cancel_sale(
    sale_id: UUID,
    data: SupplySaleCancel,
    service: InventoryServiceDependency,
    user: CurrentUser,
) -> SupplySaleRead:
    """Void a sale and give the stock back, with the reason written down.

    The stock returns as `adjustment` lines on the same lots rather than by
    erasing the `sale_out` ones: the kardex is a history, and a bottle that went
    out and came back is two things that happened.
    """
    sale = await service.cancel_sale(sale_id, data, actor=user)
    return SupplySaleRead.model_validate(sale)
