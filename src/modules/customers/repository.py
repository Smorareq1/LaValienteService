from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.customers.models import Customer


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


class CustomersRepository:
    """Persistence for customers; the search shape lives here, not in the service."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _search_filter(search: str) -> ColumnElement[bool]:
        """Match on name (case-insensitive, anywhere) or phone (digits only).

        People type phones as `4815-2964`, `48152964` or `+502 4815 2964`; the
        stored value is whatever was captured, so both sides get stripped down to
        digits before comparing.
        """
        term = search.strip()
        conditions: list[ColumnElement[bool]] = [
            func.lower(Customer.full_name).like(f"%{term.lower()}%")
        ]
        digits = _digits(term)
        if digits:
            normalized_phone = func.regexp_replace(Customer.phone, r"\D", "", "g")
            conditions.append(normalized_phone.like(f"%{digits}%"))
        return or_(*conditions)

    def _base_query(self, *, search: str | None, include_inactive: bool) -> Select[tuple[Customer]]:
        statement = select(Customer).where(Customer.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(Customer.is_active.is_(True))
        if search:
            statement = statement.where(self._search_filter(search))
        return statement

    async def search(
        self,
        *,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_inactive: bool = False,
    ) -> tuple[list[Customer], int]:
        statement = self._base_query(search=search, include_inactive=include_inactive)

        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))

        page_statement = (
            statement.order_by(func.lower(Customer.full_name))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.session.scalars(page_statement)).all())
        return items, total or 0

    async def get(self, customer_id: UUID) -> Customer | None:
        statement = select(Customer).where(
            Customer.id == customer_id, Customer.deleted_at.is_(None)
        )
        customer: Customer | None = await self.session.scalar(statement)
        return customer

    async def find_by_phone(self, phone: str) -> Customer | None:
        """Exact-digits phone match — the duplicate signal of Plan 0004 §8."""
        digits = _digits(phone)
        if not digits:
            return None
        normalized_phone = func.regexp_replace(Customer.phone, r"\D", "", "g")
        statement = (
            select(Customer)
            .where(normalized_phone == digits, Customer.deleted_at.is_(None))
            .order_by(Customer.created_at)
            .limit(1)
        )
        customer: Customer | None = await self.session.scalar(statement)
        return customer

    def add(self, customer: Customer) -> None:
        self.session.add(customer)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
