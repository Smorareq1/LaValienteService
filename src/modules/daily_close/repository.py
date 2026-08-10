from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.daily_close.models import DailyClosure


class DailyCloseRepository:
    """Persistence for the day's record of account. No rules, no arithmetic.

    Also the `ClosedDays` of `lock.py`: `is_closed` is the one question the other
    services ask before writing, and answering it needs nothing but this table.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, closure_id: UUID) -> DailyClosure | None:
        closure: DailyClosure | None = await self.session.scalar(
            select(DailyClosure).where(
                DailyClosure.id == closure_id, DailyClosure.deleted_at.is_(None)
            )
        )
        return closure

    async def get_for_date(self, day: date) -> DailyClosure | None:
        """The close that stands for a date, if the day was closed."""
        closure: DailyClosure | None = await self.session.scalar(
            select(DailyClosure).where(
                DailyClosure.close_date == day, DailyClosure.deleted_at.is_(None)
            )
        )
        return closure

    async def is_closed(self, day: date) -> bool:
        """The date lock (D9), asked as cheaply as it can be asked.

        Every write in `orders`, `expenses` and `supply_sales` goes through here,
        so it selects the date and not the row: the partial unique index answers
        it without touching the table.
        """
        found = await self.session.scalar(
            select(DailyClosure.close_date).where(
                DailyClosure.close_date == day, DailyClosure.deleted_at.is_(None)
            )
        )
        return found is not None

    async def list_between(
        self, *, since: date, until: date, include_reopened: bool = False
    ) -> list[DailyClosure]:
        statement = select(DailyClosure).where(
            DailyClosure.close_date >= since, DailyClosure.close_date <= until
        )
        if not include_reopened:
            statement = statement.where(DailyClosure.deleted_at.is_(None))
        statement = statement.order_by(DailyClosure.close_date.desc(), DailyClosure.closed_at)
        return list((await self.session.scalars(statement)).all())

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def commit(self) -> None:
        await self.session.commit()
