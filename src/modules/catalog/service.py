from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.catalog.models import (
    GarmentType,
    PricingMode,
    ServiceOption,
    ServicePrice,
    ServiceType,
)
from src.modules.catalog.repository import CatalogRepository
from src.modules.catalog.schemas import (
    GarmentTypeCreate,
    GarmentTypeRead,
    GarmentTypeUpdate,
    ServiceOptionRead,
    ServicePriceCreate,
    ServiceTypeCreate,
    ServiceTypeRead,
    ServiceTypeUpdate,
)

#: Key of a price row: a tiered service prices per option, the rest per service.
PriceKey = tuple[UUID, UUID | None]


def resolve_price(prices: list[ServicePrice], key: PriceKey, on_date: date) -> Decimal | None:
    """Pick the price in force on `on_date` for a service/option pair.

    Windows are not supposed to overlap — `register_price` closes the previous one
    before opening the next — but if a bad import ever produces an overlap, the
    most recently started window wins so the newest intent applies.
    """
    service_type_id, service_option_id = key
    candidates = [
        price
        for price in prices
        if price.service_type_id == service_type_id
        and price.service_option_id == service_option_id
        and price.covers(on_date)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda price: price.valid_from).price


class CatalogService:
    """Business rules of the catalog: price validity windows and soft-delete."""

    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    async def list_service_types(
        self, *, on_date: date, include_inactive: bool = False
    ) -> list[ServiceTypeRead]:
        """The catalog as the app needs it: services, options and prices in force."""
        service_types = await self.repository.list_service_types(include_inactive=include_inactive)
        prices = await self.repository.list_prices_on(on_date)
        return [self._to_read(service_type, prices, on_date) for service_type in service_types]

    async def get_service_type(self, service_type_id: UUID, *, on_date: date) -> ServiceTypeRead:
        service_type = await self._require_service_type(service_type_id)
        prices = await self.repository.list_prices_on(on_date, service_type_ids=[service_type_id])
        return self._to_read(service_type, prices, on_date)

    @staticmethod
    def _to_read(
        service_type: ServiceType, prices: list[ServicePrice], on_date: date
    ) -> ServiceTypeRead:
        read = ServiceTypeRead.model_validate(service_type)
        if service_type.pricing_mode is PricingMode.PER_UNIT:
            read.current_price = resolve_price(prices, (service_type.id, None), on_date)
        read.options = [
            ServiceOptionRead.model_validate(option).model_copy(
                update={
                    "current_price": resolve_price(prices, (service_type.id, option.id), on_date)
                }
            )
            for option in service_type.options
            if option.deleted_at is None and option.is_active
        ]
        return read

    async def create_service_type(self, data: ServiceTypeCreate) -> ServiceType:
        existing = await self.repository.get_service_type_by_code(data.code)
        if existing is not None:
            raise ConflictError(f"A service with code '{data.code}' already exists.")

        service_type = ServiceType(
            code=data.code,
            name=data.name,
            pricing_mode=data.pricing_mode,
            unit_label=data.unit_label,
            sort_order=data.sort_order,
        )
        service_type.options = [
            ServiceOption(
                code=option.code,
                name=option.name,
                min_quantity=option.min_quantity,
                max_quantity=option.max_quantity,
                sort_order=option.sort_order,
            )
            for option in data.options
        ]
        self.repository.add(service_type)
        await self.repository.commit()
        return service_type

    async def update_service_type(
        self, service_type_id: UUID, data: ServiceTypeUpdate
    ) -> ServiceType:
        service_type = await self._require_service_type(service_type_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(service_type, field, value)
        await self.repository.commit()
        return service_type

    async def register_price(self, service_type_id: UUID, data: ServicePriceCreate) -> ServicePrice:
        """Open a new price window, closing the one in force (Plan 0001 §5.2).

        Both writes happen in one transaction: a catalog with two open windows for
        the same service would make a ticket's total depend on query order.
        """
        service_type = await self._require_service_type(service_type_id)

        if service_type.pricing_mode is PricingMode.VARIABLE:
            raise ConflictError("A variable service takes its amount at capture time, not a price.")
        if service_type.pricing_mode is PricingMode.TIERED and data.service_option_id is None:
            raise ConflictError(
                "A tiered service prices its options, so service_option_id is required."
            )
        if service_type.pricing_mode is PricingMode.PER_UNIT and data.service_option_id is not None:
            raise ConflictError("A per-unit service has a single price, so it takes no option.")

        if data.service_option_id is not None:
            option = await self.repository.get_service_option(data.service_option_id)
            if option is None or option.service_type_id != service_type_id:
                raise NotFoundError("The service option does not belong to this service.")

        current = await self.repository.get_open_price(service_type_id, data.service_option_id)
        if current is not None:
            if data.valid_from <= current.valid_from:
                raise ConflictError(
                    "The new price must start after the one in force "
                    f"({current.valid_from.isoformat()})."
                )
            current.valid_to = data.valid_from - timedelta(days=1)

        price = ServicePrice(
            service_type_id=service_type_id,
            service_option_id=data.service_option_id,
            price=data.price,
            valid_from=data.valid_from,
        )
        self.repository.add(price)
        await self.repository.commit()
        return price

    async def list_price_history(self, service_type_id: UUID) -> list[ServicePrice]:
        await self._require_service_type(service_type_id)
        return await self.repository.list_price_history(service_type_id)

    async def list_garment_types(self, *, include_inactive: bool = False) -> list[GarmentTypeRead]:
        garment_types = await self.repository.list_garment_types(include_inactive=include_inactive)
        return [GarmentTypeRead.model_validate(garment) for garment in garment_types]

    async def create_garment_type(self, data: GarmentTypeCreate) -> GarmentType:
        existing = await self.repository.get_garment_type_by_name(data.name)
        if existing is not None:
            raise ConflictError(f"A garment type named '{data.name}' already exists.")
        garment_type = GarmentType(name=data.name, notes=data.notes, sort_order=data.sort_order)
        self.repository.add(garment_type)
        await self.repository.commit()
        return garment_type

    async def update_garment_type(
        self, garment_type_id: UUID, data: GarmentTypeUpdate
    ) -> GarmentType:
        garment_type = await self.repository.get_garment_type(garment_type_id)
        if garment_type is None:
            raise NotFoundError("Garment type not found.")
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(garment_type, field, value)
        await self.repository.commit()
        return garment_type

    async def _require_service_type(self, service_type_id: UUID) -> ServiceType:
        service_type = await self.repository.get_service_type(service_type_id)
        if service_type is None:
            raise NotFoundError("Service type not found.")
        return service_type
