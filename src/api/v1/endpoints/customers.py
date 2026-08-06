from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import BaseVersion, CustomersServiceDependency, require_permission
from src.modules.customers.schemas import (
    CustomerCreate,
    CustomerPage,
    CustomerRead,
    CustomerUpdate,
)

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.get(
    "",
    response_model=CustomerPage,
    dependencies=[Depends(require_permission("customers.read"))],
)
async def search_customers(
    service: CustomersServiceDependency,
    search: Annotated[str | None, Query(max_length=120, description="Name or phone.")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_inactive: bool = False,
) -> CustomerPage:
    return await service.search(
        search=search,
        page=page,
        page_size=page_size,
        include_inactive=include_inactive,
    )


@router.post(
    "",
    response_model=CustomerRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("customers.create"))],
)
async def create_customer(
    data: CustomerCreate, service: CustomersServiceDependency
) -> CustomerRead:
    """Create a customer.

    A likely duplicate does not block the creation — the counter cannot stop to
    resolve identity (Plan 0004 §8). The signal is surfaced to the caller.
    """
    customer, _duplicate_of = await service.create(data)
    return CustomerRead.model_validate(customer)


@router.get(
    "/{customer_id}",
    response_model=CustomerRead,
    dependencies=[Depends(require_permission("customers.read"))],
)
async def get_customer(customer_id: UUID, service: CustomersServiceDependency) -> CustomerRead:
    customer = await service.get(customer_id)
    return CustomerRead.model_validate(customer)


@router.patch(
    "/{customer_id}",
    response_model=CustomerRead,
    dependencies=[Depends(require_permission("customers.update"))],
)
async def update_customer(
    customer_id: UUID,
    data: CustomerUpdate,
    service: CustomersServiceDependency,
    base_version: BaseVersion = None,
) -> CustomerRead:
    customer = await service.update(customer_id, data, base_version=base_version)
    return CustomerRead.model_validate(customer)


@router.delete(
    "/{customer_id}",
    response_model=CustomerRead,
    dependencies=[Depends(require_permission("customers.archive"))],
)
async def archive_customer(customer_id: UUID, service: CustomersServiceDependency) -> CustomerRead:
    """Soft-delete. The row survives because orders point at it (Plan 0001 D9)."""
    customer = await service.archive(customer_id)
    return CustomerRead.model_validate(customer)
