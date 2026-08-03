from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import CurrentUser, OrdersServiceDependency, require_permission
from src.modules.orders.models import OrderStatus
from src.modules.orders.schemas import (
    OrderCancel,
    OrderCreate,
    OrderDeliver,
    OrderPage,
    OrderPaymentCreate,
    OrderRead,
    OrderStatusChange,
)

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.post(
    "",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("orders.create"))],
)
async def create_order(
    data: OrderCreate, service: OrdersServiceDependency, user: CurrentUser
) -> OrderRead:
    """Take an order.

    The body carries quantities and choices, never prices: the total is resolved
    from the catalog in force on the order's date (Plan 0001 D5). A manual
    discount additionally demands `orders.manual_discount`, which the service
    checks because the condition is the payload, not the route.
    """
    order, warnings = await service.create(data, actor=user)
    read = OrderRead.model_validate(order)
    read.warnings = warnings
    return read


@router.get(
    "",
    response_model=OrderPage,
    dependencies=[Depends(require_permission("orders.read"))],
)
async def search_orders(
    service: OrdersServiceDependency,
    order_date: Annotated[date | None, Query(alias="date")] = None,
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
    customer_id: UUID | None = None,
    search: Annotated[
        str | None, Query(max_length=120, description="Customer, booklet or daily number.")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OrderPage:
    return await service.search(
        order_date=order_date,
        status=order_status,
        customer_id=customer_id,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{order_id}",
    response_model=OrderRead,
    dependencies=[Depends(require_permission("orders.read"))],
)
async def get_order(order_id: UUID, service: OrdersServiceDependency) -> OrderRead:
    order = await service.get(order_id)
    return OrderRead.model_validate(order)


@router.post(
    "/{order_id}/status",
    response_model=OrderRead,
    dependencies=[Depends(require_permission("orders.update"))],
)
async def change_order_status(
    order_id: UUID, data: OrderStatusChange, service: OrdersServiceDependency, user: CurrentUser
) -> OrderRead:
    """Move the ticket along the chain of Plan 0001 §7.1.

    Delivery and cancellation are not reachable here — see the two endpoints
    below, which take the data each one needs.
    """
    order = await service.change_status(order_id, data.status, actor=user)
    return OrderRead.model_validate(order)


@router.post(
    "/{order_id}/deliver",
    response_model=OrderRead,
    dependencies=[Depends(require_permission("orders.deliver"))],
)
async def deliver_order(
    order_id: UUID, data: OrderDeliver, service: OrdersServiceDependency, user: CurrentUser
) -> OrderRead:
    """Hand the laundry back (§7.2).

    Garments not listed are taken to have gone back whole. Any balance left after
    the payment included here additionally demands `orders.deliver_unpaid`, which
    the service checks because the condition is the money, not the route.
    """
    order = await service.deliver(order_id, data, actor=user)
    return OrderRead.model_validate(order)


@router.post(
    "/{order_id}/cancel",
    response_model=OrderRead,
    dependencies=[Depends(require_permission("orders.cancel"))],
)
async def cancel_order(
    order_id: UUID, data: OrderCancel, service: OrdersServiceDependency, user: CurrentUser
) -> OrderRead:
    """Void a ticket while the laundry still has the clothes. Reason mandatory."""
    order = await service.cancel(order_id, data, actor=user)
    return OrderRead.model_validate(order)


@router.post(
    "/{order_id}/payments",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("orders.collect_payment"))],
)
async def add_order_payment(
    order_id: UUID, data: OrderPaymentCreate, service: OrdersServiceDependency, user: CurrentUser
) -> OrderRead:
    """Register money received. Returns the whole ticket, balance included."""
    order = await service.add_payment(order_id, data, actor=user)
    return OrderRead.model_validate(order)
