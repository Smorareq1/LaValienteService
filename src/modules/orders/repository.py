from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from uuid import UUID

from sqlalchemy import ColumnElement, Select, cast, func, or_, select
from sqlalchemy import String as SaString
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.modules.customers.models import Customer
from src.modules.orders.models import Order, OrderStatus


class OrdersRepository:
    """Persistence for orders. Holds queries and the daily correlative, no rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _with_lines() -> Select[tuple[Order]]:
        """Load the four child collections up front.

        A ticket is read as a whole — never a header without its lines — so the
        alternative is four lazy loads per row, which under an async session is
        not slow but fatal (`MissingGreenlet`).
        """
        return select(Order).options(
            selectinload(Order.garments),
            selectinload(Order.charges),
            selectinload(Order.discounts),
            selectinload(Order.payments),
        )

    async def get(self, order_id: UUID) -> Order | None:
        statement = self._with_lines().where(Order.id == order_id, Order.deleted_at.is_(None))
        order: Order | None = await self.session.scalar(statement)
        return order

    async def next_daily_number(self, order_date: date) -> int:
        """The next `No.` of the day (D4).

        Read and not a counter table: the laundry writes a few dozen tickets a
        day, and the unique constraint plus a retry is cheaper than a hot row
        (D11). Cancelled and archived tickets still count — a correlative that
        gets reused stops being a reference.
        """
        highest = await self.session.scalar(
            select(func.max(Order.daily_number)).where(Order.order_date == order_date)
        )
        return (highest or 0) + 1

    def _base_query(
        self,
        *,
        order_date: date | None,
        status: OrderStatus | None,
        customer_id: UUID | None,
        search: str | None,
    ) -> Select[tuple[Order]]:
        statement = select(Order).where(Order.deleted_at.is_(None))
        if order_date is not None:
            statement = statement.where(Order.order_date == order_date)
        if status is not None:
            statement = statement.where(Order.status == status)
        if customer_id is not None:
            statement = statement.where(Order.customer_id == customer_id)
        if search:
            statement = statement.join(Customer, Customer.id == Order.customer_id).where(
                self._search_filter(search)
            )
        return statement

    @staticmethod
    def _search_filter(search: str) -> ColumnElement[bool]:
        """Search crosses customer, booklet and correlative (Plan 0001 §8).

        Whoever is looking for a ticket has one of three things in hand: the
        customer's name, the printed booklet, or the number of the day.
        """
        term = search.strip()
        conditions: list[ColumnElement[bool]] = [
            func.lower(Customer.full_name).like(f"%{term.lower()}%"),
            cast(Order.booklet_serial, SaString).like(f"%{term}%"),
        ]
        if term.isdigit():
            conditions.append(Order.daily_number == int(term))
        return or_(*conditions)

    async def search(
        self,
        *,
        order_date: date | None = None,
        status: OrderStatus | None = None,
        customer_id: UUID | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Order], int]:
        statement = self._base_query(
            order_date=order_date, status=status, customer_id=customer_id, search=search
        )
        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))

        page_statement = (
            statement.order_by(Order.order_date.desc(), Order.daily_number.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            # Only the payments: the row shows the balance, and the rest of the
            # lines belong to the detail. Hung on the page and not on `statement`
            # so the count above stays a count.
            .options(selectinload(Order.payments))
        )
        items = list((await self.session.scalars(page_statement)).all())
        return items, total or 0

    def add(self, instance: object) -> None:
        self.session.add(instance)

    def expunge(self, instance: object) -> None:
        """Drop a staged object (and its lines) from the session."""
        self.session.expunge(instance)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Run a write that is allowed to fail without losing the transaction.

        Taking the daily correlative is a read-then-write race, so the insert
        needs to be retryable; a plain rollback would throw away everything the
        request had already done.
        """
        async with self.session.begin_nested():
            yield

    async def commit(self) -> None:
        await self.session.commit()
