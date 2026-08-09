from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, Select, cast, func, or_, select
from sqlalchemy import String as SaString
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.business_time import business_day_bounds
from src.modules.customers.models import Customer
from src.modules.orders.models import (
    Order,
    OrderCharge,
    OrderDiscount,
    OrderGarment,
    OrderPayment,
    OrderStatus,
    PaymentMethod,
)


@dataclass(frozen=True)
class DayStatusTotals:
    """One status of one day, added up. Raw material for the daily summary."""

    status: OrderStatus
    orders: int
    pieces: int
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal


class OrdersRepository:
    """Persistence for orders. Holds queries and the daily correlative, no rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _with_lines() -> Select[tuple[Order]]:
        """Load the four child collections up front, tombstones left out.

        A ticket is read as a whole — never a header without its lines — so the
        alternative is four lazy loads per row, which under an async session is
        not slow but fatal (`MissingGreenlet`).

        The `deleted_at` filter is what makes a corrected ticket read as it now
        stands: replacing a line marks the old one deleted rather than erasing
        it, so that the change reaches the devices (Plan 0004 D8).
        """
        return select(Order).options(
            selectinload(Order.garments.and_(OrderGarment.deleted_at.is_(None))),
            selectinload(Order.charges.and_(OrderCharge.deleted_at.is_(None))),
            selectinload(Order.discounts.and_(OrderDiscount.deleted_at.is_(None))),
            selectinload(Order.payments.and_(OrderPayment.deleted_at.is_(None))),
        )

    async def get(self, order_id: UUID, *, refresh: bool = False) -> Order | None:
        """The ticket with its lines.

        `refresh` reloads the collections over what the session already holds,
        which is how a ticket just corrected stops carrying the lines it had a
        moment ago: they are still in memory, marked deleted, and only a
        `populate_existing` read replaces them.
        """
        statement = self._with_lines().where(Order.id == order_id, Order.deleted_at.is_(None))
        if refresh:
            statement = statement.execution_options(populate_existing=True)
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

    async def find_by_ticket(
        self,
        *,
        booklet_serial: str | None = None,
        order_date: date | None = None,
        daily_number: int | None = None,
    ) -> list[Order]:
        """The ticket somebody is holding, by what is written on it.

        Two identifiers, and they are not equally good. `booklet_serial` is
        **printed** by the print shop and unique across the whole booklet, so a
        hit on it is the ticket. `daily_number` is handwritten and only unique
        within its day, so it needs the date to mean anything — which is why the
        pair is taken together or not at all.

        Both are tried and the results unioned rather than one falling back to
        the other: when the serial and the number point at different tickets,
        that disagreement is the useful answer, and a fallback would hide it by
        silently preferring whichever was tried first.

        Cancelled and already-delivered tickets are **not** filtered out. The
        caller is about to tell somebody at a counter what this piece of paper
        is, and "that one was handed back on Tuesday" is an answer; finding
        nothing is not.
        """
        conditions: list[ColumnElement[bool]] = []
        if booklet_serial:
            # Compared through `btrim`/`upper` and not as stored: the serial on a
            # ticket captured by hand is whatever somebody typed, `#A-0042` and
            # `a-0042` included, and a delivery that cannot find the ticket
            # because of a stray character is a worse outcome than a sequential
            # scan of a table that grows by a few dozen rows a day.
            conditions.append(
                func.upper(func.btrim(Order.booklet_serial, " #")) == booklet_serial
            )
        if order_date is not None and daily_number is not None:
            conditions.append(
                (Order.order_date == order_date) & (Order.daily_number == daily_number)
            )
        if not conditions:
            return []

        statement = (
            select(Order)
            .where(Order.deleted_at.is_(None), or_(*conditions))
            # Only the payments, for the same reason as `search`: the counter is
            # deciding what to charge, and the balance is what it needs.
            .options(selectinload(Order.payments.and_(OrderPayment.deleted_at.is_(None))))
            .order_by(Order.order_date.desc(), Order.daily_number.desc())
        )
        return list((await self.session.scalars(statement)).all())

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

    async def summarize_day(
        self, order_date: date
    ) -> tuple[list[DayStatusTotals], dict[OrderStatus, Decimal]]:
        """The day's tickets added up by status, and what was paid against them.

        Two aggregates rather than loading the orders: a busy Saturday is a few
        dozen rows today, but a summary that reads every ticket to add four
        columns is the kind of thing that stops working without warning.

        Payments come back grouped by the ticket's status so the caller can tell
        money collected on a voided ticket — which is in the drawer all the same
        — from money that settles a live one.
        """
        rows = await self.session.execute(
            select(
                Order.status,
                func.count(),
                func.coalesce(func.sum(Order.total_pieces), 0),
                func.coalesce(func.sum(Order.subtotal), Decimal("0.00")),
                func.coalesce(func.sum(Order.discount_total), Decimal("0.00")),
                func.coalesce(func.sum(Order.total), Decimal("0.00")),
            )
            .where(Order.order_date == order_date, Order.deleted_at.is_(None))
            .group_by(Order.status)
        )
        totals = [
            DayStatusTotals(
                status=status,
                orders=orders,
                pieces=pieces,
                subtotal=subtotal,
                discount_total=discount_total,
                total=total,
            )
            for status, orders, pieces, subtotal, discount_total, total in rows
        ]

        paid_rows = await self.session.execute(
            select(Order.status, func.coalesce(func.sum(OrderPayment.amount), Decimal("0.00")))
            .join(OrderPayment, OrderPayment.order_id == Order.id)
            .where(
                Order.order_date == order_date,
                Order.deleted_at.is_(None),
                OrderPayment.deleted_at.is_(None),
            )
            .group_by(Order.status)
        )
        return totals, {status: amount for status, amount in paid_rows}

    async def collected_on(self, day: date) -> list[tuple[Decimal, PaymentMethod]]:
        """Money received on a business date, whatever ticket it settles (D1).

        Cut by `paid_at` and not by the ticket's date, which is the whole of D1:
        an advance belongs to the day it was handed over and the balance to the
        day it was collected. `paid_at` is an audit timestamp in UTC, so the day
        is the laundry's — 19:00 in Cobán is already tomorrow in UTC.

        Payments against voided tickets are included on purpose: that money is in
        the drawer, and the count has to explain it.
        """
        start, end = business_day_bounds(day)
        rows = await self.session.execute(
            select(OrderPayment.amount, OrderPayment.method)
            .join(Order, Order.id == OrderPayment.order_id)
            .where(
                OrderPayment.paid_at >= start,
                OrderPayment.paid_at < end,
                OrderPayment.deleted_at.is_(None),
                Order.deleted_at.is_(None),
            )
        )
        return [(amount, method) for amount, method in rows]

    async def count_delivered_on(self, day: date) -> int:
        """Tickets handed back on a business date (§5.4 `orders_delivered`)."""
        start, end = business_day_bounds(day)
        total = await self.session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.delivered_at >= start,
                Order.delivered_at < end,
                Order.deleted_at.is_(None),
            )
        )
        return total or 0

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

    async def flush(self) -> None:
        """Send what is pending to the database without ending the transaction."""
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
