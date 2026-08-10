from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.expenses.models import Expense, ExpenseCategory, ExpenseStatus


class ExpensesRepository:
    """Persistence for categories and expenses. No rules, no totals."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- categories --------------------------------------------------------

    async def list_categories(self, *, include_inactive: bool = False) -> list[ExpenseCategory]:
        statement = select(ExpenseCategory).where(ExpenseCategory.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(ExpenseCategory.is_active.is_(True))
        statement = statement.order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
        return list((await self.session.scalars(statement)).all())

    async def get_category(self, category_id: UUID) -> ExpenseCategory | None:
        statement = select(ExpenseCategory).where(
            ExpenseCategory.id == category_id, ExpenseCategory.deleted_at.is_(None)
        )
        category: ExpenseCategory | None = await self.session.scalar(statement)
        return category

    async def get_category_by_name(self, name: str) -> ExpenseCategory | None:
        statement = select(ExpenseCategory).where(
            func.lower(ExpenseCategory.name) == name.strip().lower(),
            ExpenseCategory.deleted_at.is_(None),
        )
        category: ExpenseCategory | None = await self.session.scalar(statement)
        return category

    # -- expenses ----------------------------------------------------------

    async def list_expenses(
        self,
        *,
        expense_date: date | None = None,
        category_id: UUID | None = None,
        status: ExpenseStatus | None = None,
        employee_id: UUID | None = None,
    ) -> list[Expense]:
        statement = select(Expense).where(Expense.deleted_at.is_(None))
        if expense_date is not None:
            statement = statement.where(Expense.expense_date == expense_date)
        if category_id is not None:
            statement = statement.where(Expense.category_id == category_id)
        if status is not None:
            statement = statement.where(Expense.status == status)
        if employee_id is not None:
            statement = statement.where(Expense.employee_id == employee_id)
        statement = statement.order_by(Expense.expense_date.desc(), Expense.created_at)
        return list((await self.session.scalars(statement)).all())

    async def get_expense(self, expense_id: UUID) -> Expense | None:
        statement = select(Expense).where(
            Expense.id == expense_id, Expense.deleted_at.is_(None)
        )
        expense: Expense | None = await self.session.scalar(statement)
        return expense

    async def get_expense_including_voided(self, expense_id: UUID) -> Expense | None:
        """Voided ones included — how a re-sent `void` recognises what it did."""
        expense: Expense | None = await self.session.scalar(
            select(Expense).where(Expense.id == expense_id)
        )
        return expense

    async def expense_for_attendance(self, record_id: UUID) -> Expense | None:
        """The live payment against a working day, if the overtime was paid."""
        statement = select(Expense).where(
            Expense.attendance_record_id == record_id, Expense.deleted_at.is_(None)
        )
        expense: Expense | None = await self.session.scalar(statement)
        return expense

    # -- writes ------------------------------------------------------------

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
