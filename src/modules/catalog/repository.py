from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.modules.catalog.models import GarmentType, ServiceOption, ServicePrice, ServiceType


class CatalogRepository:
    """Persistence for the catalog. Holds no pricing rules, only queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _service_type_query() -> Select[tuple[ServiceType]]:
        return select(ServiceType).options(selectinload(ServiceType.options))

    async def list_service_types(self, *, include_inactive: bool = False) -> list[ServiceType]:
        statement = self._service_type_query().where(ServiceType.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(ServiceType.is_active.is_(True))
        statement = statement.order_by(ServiceType.sort_order, ServiceType.name)
        return list((await self.session.scalars(statement)).all())

    async def get_service_type(self, service_type_id: UUID) -> ServiceType | None:
        statement = self._service_type_query().where(
            ServiceType.id == service_type_id, ServiceType.deleted_at.is_(None)
        )
        service_type: ServiceType | None = await self.session.scalar(statement)
        return service_type

    async def get_service_type_by_code(self, code: str) -> ServiceType | None:
        statement = self._service_type_query().where(
            ServiceType.code == code, ServiceType.deleted_at.is_(None)
        )
        service_type: ServiceType | None = await self.session.scalar(statement)
        return service_type

    async def get_service_option(self, option_id: UUID) -> ServiceOption | None:
        statement = select(ServiceOption).where(
            ServiceOption.id == option_id, ServiceOption.deleted_at.is_(None)
        )
        option: ServiceOption | None = await self.session.scalar(statement)
        return option

    async def list_prices_on(
        self, on_date: date, *, service_type_ids: list[UUID] | None = None
    ) -> list[ServicePrice]:
        """Prices whose validity window covers `on_date`.

        One query for the whole catalog: pricing a ticket must not fan out into a
        lookup per line.
        """
        statement = select(ServicePrice).where(
            ServicePrice.deleted_at.is_(None),
            ServicePrice.valid_from <= on_date,
            (ServicePrice.valid_to.is_(None)) | (ServicePrice.valid_to >= on_date),
        )
        if service_type_ids is not None:
            statement = statement.where(ServicePrice.service_type_id.in_(service_type_ids))
        return list((await self.session.scalars(statement)).all())

    async def list_price_history(self, service_type_id: UUID) -> list[ServicePrice]:
        statement = (
            select(ServicePrice)
            .where(
                ServicePrice.service_type_id == service_type_id,
                ServicePrice.deleted_at.is_(None),
            )
            .order_by(ServicePrice.valid_from.desc())
        )
        return list((await self.session.scalars(statement)).all())

    async def get_open_price(
        self, service_type_id: UUID, service_option_id: UUID | None
    ) -> ServicePrice | None:
        """The still-open price window for a service (or one of its options)."""
        statement = select(ServicePrice).where(
            ServicePrice.service_type_id == service_type_id,
            ServicePrice.service_option_id.is_(service_option_id)
            if service_option_id is None
            else ServicePrice.service_option_id == service_option_id,
            ServicePrice.valid_to.is_(None),
            ServicePrice.deleted_at.is_(None),
        )
        price: ServicePrice | None = await self.session.scalar(statement)
        return price

    async def list_garment_types(self, *, include_inactive: bool = False) -> list[GarmentType]:
        statement = select(GarmentType).where(GarmentType.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(GarmentType.is_active.is_(True))
        statement = statement.order_by(GarmentType.sort_order, GarmentType.name)
        return list((await self.session.scalars(statement)).all())

    async def get_garment_type(self, garment_type_id: UUID) -> GarmentType | None:
        statement = select(GarmentType).where(
            GarmentType.id == garment_type_id, GarmentType.deleted_at.is_(None)
        )
        garment_type: GarmentType | None = await self.session.scalar(statement)
        return garment_type

    async def get_garment_type_by_name(self, name: str) -> GarmentType | None:
        statement = select(GarmentType).where(
            GarmentType.name == name, GarmentType.deleted_at.is_(None)
        )
        garment_type: GarmentType | None = await self.session.scalar(statement)
        return garment_type

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
