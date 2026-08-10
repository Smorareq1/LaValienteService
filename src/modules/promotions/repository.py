from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.promotions.models import Promotion


class PromotionsRepository:
    """Persistence for promotions. Holds no discount arithmetic, only queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, *, include_inactive: bool = False) -> list[Promotion]:
        statement = select(Promotion).where(Promotion.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(Promotion.is_active.is_(True))
        statement = statement.order_by(Promotion.valid_from.desc(), Promotion.name)
        return list((await self.session.scalars(statement)).all())

    async def list_active_on(self, on_date: date) -> list[Promotion]:
        """The promotions in force on `on_date`.

        One query for all of them: pricing a ticket must not fan out into a
        lookup per discount, and the app pulls the whole live set at once to show
        its chips (Plan 0006 §5.2).
        """
        statement = (
            select(Promotion)
            .where(
                Promotion.deleted_at.is_(None),
                Promotion.is_active.is_(True),
                Promotion.valid_from <= on_date,
                (Promotion.valid_to.is_(None)) | (Promotion.valid_to >= on_date),
            )
            .order_by(Promotion.name)
        )
        return list((await self.session.scalars(statement)).all())

    async def get(self, promotion_id: UUID) -> Promotion | None:
        statement = select(Promotion).where(
            Promotion.id == promotion_id, Promotion.deleted_at.is_(None)
        )
        promotion: Promotion | None = await self.session.scalar(statement)
        return promotion

    async def get_by_code(self, code: str) -> Promotion | None:
        statement = select(Promotion).where(
            Promotion.code == code, Promotion.deleted_at.is_(None)
        )
        promotion: Promotion | None = await self.session.scalar(statement)
        return promotion

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def commit(self) -> None:
        await self.session.commit()
