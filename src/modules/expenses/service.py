from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.core.money import money
from src.modules.daily_close.lock import ClosedDays, ensure_open
from src.modules.daily_close.totals import Split
from src.modules.expenses.models import (
    SUPPLY_PURCHASE_CATEGORY,
    Expense,
    ExpenseCategory,
    ExpenseStatus,
)
from src.modules.expenses.repository import ExpensesRepository
from src.modules.expenses.schemas import (
    ExpenseCategoryCreate,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseRead,
    ExpensesDaySummary,
    ExpenseUpdate,
    ExpenseVoid,
)
from src.modules.identity.models import User
from src.modules.inventory.repository import InventoryRepository
from src.modules.orders.models import PaymentMethod
from src.modules.staff.repository import StaffRepository

ZERO = Decimal("0.00")


class ExpensesService:
    """The right-hand side of the sheet: what went out, on what, and for whom.

    It reaches into `staff` and `inventory` to check the links it stores, and
    never through them: an expense that names an employee who does not exist, or
    a lot that was never received, is a number nobody can account for later.
    """

    def __init__(
        self,
        repository: ExpensesRepository,
        staff: StaffRepository,
        inventory: InventoryRepository,
        closed_days: ClosedDays | None = None,
    ) -> None:
        self.repository = repository
        self.staff = staff
        self.inventory = inventory
        #: The date lock of D9. Every write here lands on a day's sheet, so all
        #: three of them consult it.
        self.closed_days = closed_days

    # -- categories --------------------------------------------------------

    async def list_categories(self, *, include_inactive: bool = False) -> list[ExpenseCategory]:
        return await self.repository.list_categories(include_inactive=include_inactive)

    async def create_category(self, data: ExpenseCategoryCreate) -> ExpenseCategory:
        await self._check_category_name(data.name, category_id=None)
        category = ExpenseCategory(name=data.name.strip(), sort_order=data.sort_order)
        self.repository.add(category)
        await self.repository.commit()
        return category

    async def update_category(
        self, category_id: UUID, data: ExpenseCategoryUpdate
    ) -> ExpenseCategory:
        category = await self._require_category(category_id, must_be_active=False)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("name") is not None:
            await self._check_category_name(changes["name"], category_id=category_id)
            changes["name"] = changes["name"].strip()

        for field, value in changes.items():
            setattr(category, field, value)

        # Deactivating keeps the category readable: last month's expenses point
        # at it and a category that vanished would leave them uncategorised.
        await self.repository.commit()
        return category

    async def _check_category_name(self, name: str, *, category_id: UUID | None) -> None:
        existing = await self.repository.get_category_by_name(name)
        if existing is not None and existing.id != category_id:
            raise ConflictError(f"A category named '{existing.name}' already exists.")

    async def _require_category(
        self, category_id: UUID, *, must_be_active: bool
    ) -> ExpenseCategory:
        category = await self.repository.get_category(category_id)
        if category is None:
            raise NotFoundError("Expense category not found.")
        if must_be_active and not category.is_active:
            raise ConflictError(f"The category '{category.name}' is no longer in use.")
        return category

    # -- expenses ----------------------------------------------------------

    async def list_expenses(
        self,
        *,
        expense_date: date | None = None,
        category_id: UUID | None = None,
        status: ExpenseStatus | None = None,
        employee_id: UUID | None = None,
    ) -> list[ExpenseRead]:
        expenses = await self.repository.list_expenses(
            expense_date=expense_date,
            category_id=category_id,
            status=status,
            employee_id=employee_id,
        )
        return await self._read_all(expenses)

    async def get_expense(self, expense_id: UUID) -> Expense:
        expense = await self.repository.get_expense(expense_id)
        if expense is None:
            raise NotFoundError("Expense not found.")
        return expense

    async def create_expense(self, data: ExpenseCreate, *, actor: User) -> ExpenseRead:
        """Write down money going out (§6.1).

        Everything the row points at is checked first, because a foreign-key
        violation says the same thing in a language nobody at the counter can
        act on.
        """
        if data.id is not None and (
            await self.repository.get_expense_including_voided(data.id) is not None
        ):
            raise ConflictError("That expense is already registered.")

        expense_date = data.expense_date or business_date()
        await ensure_open(self.closed_days, expense_date)
        category = await self._require_category(data.category_id, must_be_active=True)
        await self._check_links(data)

        expense = Expense(
            id=data.id or uuid4(),
            expense_date=expense_date,
            category_id=category.id,
            concept=data.concept.strip(),
            amount=money(data.amount),
            method=data.method,
            status=data.status,
            employee_id=data.employee_id,
            attendance_record_id=data.attendance_record_id,
            product_lot_id=data.product_lot_id,
            observations=data.observations,
            created_by_id=actor.id,
        )
        self.repository.add(expense)
        await self.repository.commit()
        return await self._read_one(expense, category)

    async def _check_links(self, data: ExpenseCreate) -> None:
        """The three optional links, each one real (§5.3)."""
        if data.employee_id is not None:
            employee = await self.staff.get_employee(data.employee_id)
            if employee is None:
                raise NotFoundError("That employee does not exist.")

        if data.attendance_record_id is not None:
            record = await self.staff.get_attendance(data.attendance_record_id)
            if record is None:
                raise NotFoundError("That working day does not exist.")
            if record.employee_id != data.employee_id:
                raise ConflictError("That working day belongs to a different employee.")
            already = await self.repository.expense_for_attendance(data.attendance_record_id)
            if already is not None:
                # The amount is prefilled from the record, so a second payment
                # looks exactly as right as the first one did (§6.2 step 4).
                raise ConflictError(
                    f"That working day was already paid: {already.concept} "
                    f"for {already.amount}."
                )

        if data.product_lot_id is not None and await self.inventory.get_lot(
            data.product_lot_id
        ) is None:
            raise NotFoundError("That product lot does not exist.")

    async def update_expense(
        self, expense_id: UUID, data: ExpenseUpdate, *, base_version: int | None = None
    ) -> ExpenseRead:
        """Correct what was written down.

        The links are absent from `ExpenseUpdate` on purpose: moving a payment
        onto a different working day is not a correction, it is a different
        payment.
        """
        expense = await self.get_expense(expense_id)
        if base_version is not None and expense.version != base_version:
            raise StaleVersionError(
                f"That expense changed since version {base_version} "
                f"(current version is {expense.version})."
            )
        changes = data.model_dump(exclude_unset=True)

        # Both dates, and in this order (D9): the day it is leaving must be open
        # to lose it, and the day it is moving to must be open to gain it.
        # Checking only one would let a closed day be balanced by dragging an
        # expense across the line.
        await ensure_open(self.closed_days, expense.expense_date)
        moved_to = changes.get("expense_date")
        if moved_to is not None and moved_to != expense.expense_date:
            await ensure_open(self.closed_days, moved_to)

        category = None
        if changes.get("category_id") is not None:
            category = await self._require_category(changes["category_id"], must_be_active=True)
        if changes.get("amount") is not None:
            changes["amount"] = money(changes["amount"])
        if changes.get("concept") is not None:
            changes["concept"] = changes["concept"].strip()

        for field, value in changes.items():
            setattr(expense, field, value)

        await self.repository.commit()
        return await self._read_one(expense, category)

    async def void_expense(
        self, expense_id: UUID, data: ExpenseVoid, *, actor: User
    ) -> ExpenseRead:
        """Take an expense off the day, with a name and a reason on it (§7).

        A tombstone and not a delete: a device that already pulled this row has
        to learn it is gone (Plan 0004 D8), and the day's total has to be
        explainable to whoever counted the drawer.
        """
        expense = await self.get_expense(expense_id)
        await ensure_open(self.closed_days, expense.expense_date)
        expense.deleted_at = datetime.now(UTC)
        expense.voided_by_id = actor.id
        expense.void_reason = data.reason
        await self.repository.commit()
        return await self._read_one(expense, None)

    # -- the day added up --------------------------------------------------

    async def day_summary(self, expense_date: date) -> ExpensesDaySummary:
        """Σ of the day's expenses, split the ways the close needs (§6.1)."""
        expenses = await self.repository.list_expenses(expense_date=expense_date)
        categories = {
            category.id: category.name
            for category in await self.repository.list_categories(include_inactive=True)
        }

        by_category: dict[str, Decimal] = {}
        for expense in expenses:
            name = categories.get(expense.category_id, "—")
            by_category[name] = by_category.get(name, ZERO) + expense.amount

        return ExpensesDaySummary(
            expense_date=expense_date,
            total=self._sum(expenses),
            paid_total=self._sum(e for e in expenses if e.status is ExpenseStatus.PAID),
            pending_total=self._sum(e for e in expenses if e.status is ExpenseStatus.PENDING),
            cash_total=self._sum(e for e in expenses if e.method is PaymentMethod.CASH),
            transfer_total=self._sum(e for e in expenses if e.method is PaymentMethod.TRANSFER),
            count=len(expenses),
            by_category=by_category,
        )

    @staticmethod
    def _sum(expenses: Iterable[Expense]) -> Decimal:
        return money(sum((expense.amount for expense in expenses), ZERO))

    async def expenses_total(self, on_date: date) -> Decimal:
        """What the day cost, pending ones included. Read by the daily close.

        The paper counts an expense on the day it was incurred even when it is
        settled later — that is what `pending` is for (§5.3) — so this is the
        figure that goes against the day's income.
        """
        return self._sum(await self.repository.list_expenses(expense_date=on_date))

    async def paid_expenses(self, on_date: date) -> Split:
        """What actually left, split by method — the other half of the arqueo.

        Deliberately not the same set as `expenses_total`: money still owed
        never came out of the drawer, and counting it against the cash would
        leave whoever counts the box looking for notes that are still there.
        """
        expenses = await self.repository.list_expenses(
            expense_date=on_date, status=ExpenseStatus.PAID
        )
        return Split.of((expense.amount, expense.method) for expense in expenses)

    # -- booked by other modules -------------------------------------------

    async def stage_lot_purchase(
        self,
        *,
        lot_id: UUID,
        concept: str,
        amount: Decimal,
        expense_date: date,
        method: PaymentMethod,
        pending: bool,
        observations: str | None,
        actor: User,
    ) -> None:
        """Book the purchase that brought a lot in (§6.3).

        Staged and not committed: the lot, its kardex entry and this expense are
        one event, and `inventory` closes the transaction around all three. A
        purchase saved without its stock, or stock without the money that paid
        for it, is worse than a request that failed.
        """
        category = await self.repository.get_category_by_name(SUPPLY_PURCHASE_CATEGORY)
        if category is None:
            raise ConflictError(
                f"There is no '{SUPPLY_PURCHASE_CATEGORY}' category to book the purchase under."
            )
        self.repository.add(
            Expense(
                expense_date=expense_date,
                category_id=category.id,
                concept=concept,
                amount=money(amount),
                method=method,
                status=ExpenseStatus.PENDING if pending else ExpenseStatus.PAID,
                product_lot_id=lot_id,
                observations=observations,
                created_by_id=actor.id,
            )
        )

    # -- reads -------------------------------------------------------------

    async def _read_one(self, expense: Expense, category: ExpenseCategory | None) -> ExpenseRead:
        if category is None or category.id != expense.category_id:
            category = await self._require_category(expense.category_id, must_be_active=False)
        employee_name = None
        if expense.employee_id is not None:
            employee = await self.staff.get_employee(expense.employee_id)
            employee_name = employee.full_name if employee is not None else None
        return self._to_read(expense, category.name, employee_name)

    async def _read_all(self, expenses: Sequence[Expense]) -> list[ExpenseRead]:
        """Names resolved with two queries, not two per row.

        A month of expenses is a few hundred rows over a handful of categories
        and one small staff; looking either up per line would turn the screen
        into a fan-out for values that barely change.
        """
        categories = {
            category.id: category.name
            for category in await self.repository.list_categories(include_inactive=True)
        }
        employees = {
            person.id: person.full_name
            for person in await self.staff.list_employees(include_inactive=True)
        }
        return [
            self._to_read(
                expense,
                categories.get(expense.category_id, "—"),
                employees.get(expense.employee_id) if expense.employee_id else None,
            )
            for expense in expenses
        ]

    @staticmethod
    def _to_read(
        expense: Expense, category_name: str, employee_name: str | None
    ) -> ExpenseRead:
        return ExpenseRead(
            id=expense.id,
            expense_date=expense.expense_date,
            category_id=expense.category_id,
            category_name=category_name,
            concept=expense.concept,
            amount=expense.amount,
            method=expense.method,
            status=expense.status,
            employee_id=expense.employee_id,
            employee_name=employee_name,
            attendance_record_id=expense.attendance_record_id,
            product_lot_id=expense.product_lot_id,
            observations=expense.observations,
            created_by_id=expense.created_by_id,
            version=expense.version,
        )
