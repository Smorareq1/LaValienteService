from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import CatalogServiceDependency, require_permission
from src.modules.catalog.schemas import (
    GarmentTypeCreate,
    GarmentTypeRead,
    GarmentTypeUpdate,
    ServicePriceCreate,
    ServicePriceRead,
    ServiceTypeCreate,
    ServiceTypeRead,
    ServiceTypeUpdate,
)

router = APIRouter(prefix="/catalog", tags=["Catalog"])

#: Prices are date-scoped, so every read resolves against a business date; the
#: app passes the order's date when pricing a past ticket.
OnDate = Annotated[date | None, Query(description="Business date to price against.")]


@router.get(
    "/service-types",
    response_model=list[ServiceTypeRead],
    dependencies=[Depends(require_permission("catalog.read"))],
)
async def list_service_types(
    service: CatalogServiceDependency,
    on_date: OnDate = None,
    include_inactive: bool = False,
) -> list[ServiceTypeRead]:
    return await service.list_service_types(
        on_date=on_date or date.today(), include_inactive=include_inactive
    )


@router.get(
    "/service-types/{service_type_id}",
    response_model=ServiceTypeRead,
    dependencies=[Depends(require_permission("catalog.read"))],
)
async def get_service_type(
    service_type_id: UUID,
    service: CatalogServiceDependency,
    on_date: OnDate = None,
) -> ServiceTypeRead:
    return await service.get_service_type(service_type_id, on_date=on_date or date.today())


@router.post(
    "/service-types",
    response_model=ServiceTypeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("catalog.manage"))],
)
async def create_service_type(
    data: ServiceTypeCreate, service: CatalogServiceDependency
) -> ServiceTypeRead:
    created = await service.create_service_type(data)
    return await service.get_service_type(created.id, on_date=date.today())


@router.patch(
    "/service-types/{service_type_id}",
    response_model=ServiceTypeRead,
    dependencies=[Depends(require_permission("catalog.manage"))],
)
async def update_service_type(
    service_type_id: UUID,
    data: ServiceTypeUpdate,
    service: CatalogServiceDependency,
) -> ServiceTypeRead:
    await service.update_service_type(service_type_id, data)
    return await service.get_service_type(service_type_id, on_date=date.today())


@router.get(
    "/service-types/{service_type_id}/prices",
    response_model=list[ServicePriceRead],
    dependencies=[Depends(require_permission("catalog.read"))],
)
async def list_price_history(
    service_type_id: UUID, service: CatalogServiceDependency
) -> list[ServicePriceRead]:
    prices = await service.list_price_history(service_type_id)
    return [ServicePriceRead.model_validate(price) for price in prices]


@router.post(
    "/service-types/{service_type_id}/prices",
    response_model=ServicePriceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("catalog.manage"))],
)
async def register_price(
    service_type_id: UUID,
    data: ServicePriceCreate,
    service: CatalogServiceDependency,
) -> ServicePriceRead:
    """Open a new price window; the one in force is closed the day before."""
    price = await service.register_price(service_type_id, data)
    return ServicePriceRead.model_validate(price)


@router.get(
    "/garment-types",
    response_model=list[GarmentTypeRead],
    dependencies=[Depends(require_permission("catalog.read"))],
)
async def list_garment_types(
    service: CatalogServiceDependency, include_inactive: bool = False
) -> list[GarmentTypeRead]:
    return await service.list_garment_types(include_inactive=include_inactive)


@router.post(
    "/garment-types",
    response_model=GarmentTypeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("catalog.manage"))],
)
async def create_garment_type(
    data: GarmentTypeCreate, service: CatalogServiceDependency
) -> GarmentTypeRead:
    garment_type = await service.create_garment_type(data)
    return GarmentTypeRead.model_validate(garment_type)


@router.patch(
    "/garment-types/{garment_type_id}",
    response_model=GarmentTypeRead,
    dependencies=[Depends(require_permission("catalog.manage"))],
)
async def update_garment_type(
    garment_type_id: UUID,
    data: GarmentTypeUpdate,
    service: CatalogServiceDependency,
) -> GarmentTypeRead:
    garment_type = await service.update_garment_type(garment_type_id, data)
    return GarmentTypeRead.model_validate(garment_type)
