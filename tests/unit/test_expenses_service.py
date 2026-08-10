"""The right-hand side of the sheet (Plan 0005 §5.3, §6.1, §6.2 step 4).

What is exercised here is what an expense is *allowed* to point at, that a
working day is paid once, that voiding leaves a name and a reason behind, and
that the day adds up to the same figure the paper does.

No database: the three fake repositories below stand in for one session.
"""

from datetime import date, time
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.modules.expenses.models import (
    OVERTIME_CATEGORY,
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
    ExpenseUpdate,
    ExpenseVoid,
)
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.inventory.models import ProductLot
from src.modules.inventory.repository import InventoryRepository
from src.modules.orders.models import PaymentMethod
from src.modules.staff.models import AttendanceRecord, Employee
from src.modules.staff.repository import StaffRepository

DAY = date(2026, 7, 18)
ACTOR_ID = uuid4()


class FakeUser:
    id = ACTOR_ID


ACTOR = cast(User, FakeUser())


class FakeExpensesRepository:
    def __init__(self) -> None:
        self.categories: list[ExpenseCategory] = []
        self.expenses: list[Expense] = []

    async def list_categories(self, *, include_inactive: bool = False) -> list[ExpenseCategory]:
        return [c for c in self.categories if include_inactive or c.is_active]

    async def get_category(self, category_id: UUID) -> ExpenseCategory | None:
        return next((c for c in self.categories if c.id == category_id), None)

    async def get_category_by_name(self, name: str) -> ExpenseCategory | None:
        wanted = name.strip().lower()
        return next((c for c in self.categories if c.name.strip().lower() == wanted), None)

    async def list_expenses(
        self,
        *,
        expense_date: date | None = None,
        category_id: UUID | None = None,
        status: ExpenseStatus | None = None,
        employee_id: UUID | None = None,
    ) -> list[Expense]:
        return [
            expense
            for expense in self.expenses
            if expense.deleted_at is None
            and (expense_date is None or expense.expense_date == expense_date)
            and (category_id is None or expense.category_id == category_id)
            and (status is None or expense.status is status)
            and (employee_id is None or expense.employee_id == employee_id)
        ]

    async def get_expense(self, expense_id: UUID) -> Expense | None:
        return next(
            (e for e in self.expenses if e.id == expense_id and e.deleted_at is None), None
        )

    async def get_expense_including_voided(self, expense_id: UUID) -> Expense | None:
        return next((e for e in self.expenses if e.id == expense_id), None)

    async def expense_for_attendance(self, record_id: UUID) -> Expense | None:
        return next(
            (
                e
                for e in self.expenses
                if e.attendance_record_id == record_id and e.deleted_at is None
            ),
            None,
        )

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = uuid4()
        if getattr(instance, "version", None) is None:
            instance.version = 1
        if isinstance(instance, ExpenseCategory):
            self.categories.append(instance)
        elif isinstance(instance, Expense):
            self.expenses.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeStaffRepository:
    def __init__(self) -> None:
        self.employees: list[Employee] = []
        self.attendance: list[AttendanceRecord] = []

    async def get_employee(self, employee_id: UUID) -> Employee | None:
        return next((e for e in self.employees if e.id == employee_id), None)

    async def list_employees(self, *, include_inactive: bool = False) -> list[Employee]:
        return [e for e in self.employees if include_inactive or e.is_active]

    async def get_attendance(self, record_id: UUID) -> AttendanceRecord | None:
        return next((r for r in self.attendance if r.id == record_id), None)


class FakeInventoryRepository:
    def __init__(self) -> None:
        self.lots: list[ProductLot] = []

    async def get_lot(self, lot_id: UUID) -> ProductLot | None:
        return next((lot for lot in self.lots if lot.id == lot_id), None)


class FakeClosedDays:
    """The `ClosedDays` of `daily_close/lock.py`."""

    def __init__(self, *days: date) -> None:
        self.days = set(days)

    async def is_closed(self, day: date) -> bool:
        return day in self.days


def make_service(*, closed: FakeClosedDays | None = None) -> tuple[
    ExpensesService, FakeExpensesRepository, FakeStaffRepository, FakeInventoryRepository
]:
    expenses = FakeExpensesRepository()
    staff = FakeStaffRepository()
    inventory = FakeInventoryRepository()
    service = ExpensesService(
        cast(ExpensesRepository, expenses),
        cast(StaffRepository, staff),
        cast(InventoryRepository, inventory),
        # No cast, unlike the three above: `ClosedDays` is a Protocol, so the
        # fake satisfies it by having the method.
        closed,
    )
    return service, expenses, staff, inventory


def make_category(
    repository: FakeExpensesRepository, name: str, *, active: bool = True, order: int = 1
) -> ExpenseCategory:
    category = ExpenseCategory(name=name, sort_order=order)
    category.id = uuid4()
    category.version = 1
    category.is_active = active
    repository.categories.append(category)
    return category


def make_employee(repository: FakeStaffRepository, name: str = "Claudia") -> Employee:
    employee = Employee(full_name=name)
    employee.id = uuid4()
    employee.version = 1
    employee.is_active = True
    repository.employees.append(employee)
    return employee


def make_attendance(repository: FakeStaffRepository, employee: Employee) -> AttendanceRecord:
    record = AttendanceRecord(
        employee_id=employee.id,
        work_date=DAY,
        clock_in=time(6, 50),
        clock_out=time(13, 0),
        overtime_minutes=30,
    )
    record.id = uuid4()
    record.version = 1
    repository.attendance.append(record)
    return record


class TestRecording:
    async def test_an_expense_carries_its_category_and_who_wrote_it(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")

        read = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=gas.id,
                concept="Gas — 2 sacos",
                amount=Decimal("161.00"),
            ),
            actor=ACTOR,
        )

        assert read.category_name == "Gas"
        assert read.created_by_id == ACTOR_ID
        assert read.amount == Decimal("161.00")
        assert read.status is ExpenseStatus.PAID

    async def test_an_unknown_category_is_not_found(self) -> None:
        service, _, _, _ = make_service()

        with pytest.raises(NotFoundError):
            await service.create_expense(
                ExpenseCreate(category_id=uuid4(), concept="Gas", amount=Decimal("1.00")),
                actor=ACTOR,
            )

    async def test_a_category_out_of_use_takes_no_new_expenses(self) -> None:
        service, repository, _, _ = make_service()
        old = make_category(repository, "Fichas de secadora", active=False)

        with pytest.raises(ConflictError, match="no longer in use"):
            await service.create_expense(
                ExpenseCreate(category_id=old.id, concept="Fichas", amount=Decimal("20.00")),
                actor=ACTOR,
            )

    async def test_resending_an_expense_the_device_already_sent_is_a_conflict(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        expense_id = uuid4()
        body = ExpenseCreate(
            id=expense_id, category_id=gas.id, concept="Gas", amount=Decimal("161.00")
        )

        await service.create_expense(body, actor=ACTOR)
        with pytest.raises(ConflictError, match="already registered"):
            await service.create_expense(body, actor=ACTOR)

    async def test_a_lot_that_does_not_exist_cannot_be_paid_for(self) -> None:
        service, repository, _, _ = make_service()
        purchases = make_category(repository, SUPPLY_PURCHASE_CATEGORY)

        with pytest.raises(NotFoundError, match="lot"):
            await service.create_expense(
                ExpenseCreate(
                    category_id=purchases.id,
                    concept="Detergente",
                    amount=Decimal("10.00"),
                    product_lot_id=uuid4(),
                ),
                actor=ACTOR,
            )


class TestOvertime:
    async def test_paying_a_working_day_names_the_person_and_the_day(self) -> None:
        service, repository, staff, _ = make_service()
        category = make_category(repository, OVERTIME_CATEGORY)
        employee = make_employee(staff)
        record = make_attendance(staff, employee)

        read = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=category.id,
                concept="Claudia — hora extra",
                amount=Decimal("10.00"),
                employee_id=employee.id,
                attendance_record_id=record.id,
            ),
            actor=ACTOR,
        )

        assert read.employee_name == "Claudia"
        assert read.attendance_record_id == record.id
        # Half an hour at Q20 is the "Claudia Extra Q10" of the paper sheet.
        assert read.amount == Decimal("10.00")

    async def test_the_same_working_day_cannot_be_paid_twice(self) -> None:
        """The amount is prefilled from the record, so the second payment looks
        exactly as right as the first one did."""
        service, repository, staff, _ = make_service()
        category = make_category(repository, OVERTIME_CATEGORY)
        employee = make_employee(staff)
        record = make_attendance(staff, employee)
        body = ExpenseCreate(
            category_id=category.id,
            concept="Claudia — hora extra",
            amount=Decimal("10.00"),
            employee_id=employee.id,
            attendance_record_id=record.id,
        )

        await service.create_expense(body, actor=ACTOR)
        with pytest.raises(ConflictError, match="already paid"):
            await service.create_expense(body, actor=ACTOR)

    async def test_voiding_the_payment_frees_the_day_to_be_paid_properly(self) -> None:
        service, repository, staff, _ = make_service()
        category = make_category(repository, OVERTIME_CATEGORY)
        employee = make_employee(staff)
        record = make_attendance(staff, employee)
        body = ExpenseCreate(
            category_id=category.id,
            concept="Claudia — hora extra",
            amount=Decimal("10.00"),
            employee_id=employee.id,
            attendance_record_id=record.id,
        )
        first = await service.create_expense(body, actor=ACTOR)

        await service.void_expense(first.id, ExpenseVoid(reason="Monto equivocado"), actor=ACTOR)
        again = await service.create_expense(body.model_copy(update={"id": None}), actor=ACTOR)

        assert again.amount == Decimal("10.00")

    async def test_a_working_day_of_someone_else_is_refused(self) -> None:
        service, repository, staff, _ = make_service()
        category = make_category(repository, OVERTIME_CATEGORY)
        claudia = make_employee(staff, "Claudia")
        other = make_employee(staff, "Marta")
        record = make_attendance(staff, claudia)

        with pytest.raises(ConflictError, match="different employee"):
            await service.create_expense(
                ExpenseCreate(
                    category_id=category.id,
                    concept="Marta — hora extra",
                    amount=Decimal("10.00"),
                    employee_id=other.id,
                    attendance_record_id=record.id,
                ),
                actor=ACTOR,
            )

    def test_a_working_day_is_paid_to_somebody(self) -> None:
        with pytest.raises(ValueError, match="send the employee"):
            ExpenseCreate(
                category_id=uuid4(),
                concept="Hora extra",
                amount=Decimal("10.00"),
                attendance_record_id=uuid4(),
            )

    async def test_an_employee_who_does_not_exist_is_not_found(self) -> None:
        service, repository, _, _ = make_service()
        category = make_category(repository, OVERTIME_CATEGORY)

        with pytest.raises(NotFoundError, match="employee"):
            await service.create_expense(
                ExpenseCreate(
                    category_id=category.id,
                    concept="Hora extra",
                    amount=Decimal("10.00"),
                    employee_id=uuid4(),
                ),
                actor=ACTOR,
            )


class TestVoiding:
    async def test_voiding_leaves_a_name_and_a_reason(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        created = await service.create_expense(
            ExpenseCreate(category_id=gas.id, concept="Gas", amount=Decimal("161.00")),
            actor=ACTOR,
        )

        await service.void_expense(created.id, ExpenseVoid(reason="Duplicado"), actor=ACTOR)

        stored = repository.expenses[0]
        assert stored.deleted_at is not None
        assert stored.voided_by_id == ACTOR_ID
        assert stored.void_reason == "Duplicado"

    async def test_a_voided_expense_stops_counting(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        first = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY, category_id=gas.id, concept="Gas", amount=Decimal("161.00")
            ),
            actor=ACTOR,
        )
        await service.create_expense(
            ExpenseCreate(
                expense_date=DAY, category_id=gas.id, concept="Gas", amount=Decimal("20.00")
            ),
            actor=ACTOR,
        )

        await service.void_expense(first.id, ExpenseVoid(reason="Duplicado"), actor=ACTOR)

        assert await service.expenses_total(DAY) == Decimal("20.00")

    async def test_voiding_something_that_is_gone_is_not_found(self) -> None:
        service, _, _, _ = make_service()

        with pytest.raises(NotFoundError):
            await service.void_expense(uuid4(), ExpenseVoid(reason="Nada"), actor=ACTOR)


class TestTheDay:
    async def test_the_day_adds_up_to_the_paper_sheet(self) -> None:
        """The six expenses of 18/07/26 come to Q231 (§6.1), which is what the
        real sheet says. If this ever changes, the arithmetic moved, not the day."""
        service, repository, staff, _ = make_service()
        overtime = make_category(repository, OVERTIME_CATEGORY)
        gas = make_category(repository, "Gas", order=2)
        transport = make_category(repository, "Transporte (moto)", order=3)
        supplies = make_category(repository, SUPPLY_PURCHASE_CATEGORY, order=4)
        upkeep = make_category(repository, "Mantenimiento de equipo", order=5)
        claudia = make_employee(staff)

        sheet = [
            (overtime, "Claudia — hora extra", "10.00"),
            (upkeep, "Secadora", "20.00"),
            (transport, "Servicio de moto", "10.00"),
            (gas, "Gas — 2 sacos", "161.00"),
            (supplies, "Detergente", "10.00"),
            (overtime, "Hora extra", "20.00"),
        ]
        for category, concept, amount in sheet:
            await service.create_expense(
                ExpenseCreate(
                    expense_date=DAY,
                    category_id=category.id,
                    concept=concept,
                    amount=Decimal(amount),
                    employee_id=claudia.id if category is overtime else None,
                ),
                actor=ACTOR,
            )

        summary = await service.day_summary(DAY)

        assert summary.total == Decimal("231.00")
        assert summary.count == 6
        assert summary.by_category[OVERTIME_CATEGORY] == Decimal("30.00")
        assert summary.by_category["Gas"] == Decimal("161.00")

    async def test_what_is_owed_is_counted_but_told_apart(self) -> None:
        """"Pago atrasado" is still the day's expense — the paper counts it — but
        the drawer has not seen it."""
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=gas.id,
                concept="Gas pagado",
                amount=Decimal("100.00"),
            ),
            actor=ACTOR,
        )
        await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=gas.id,
                concept="Gas por pagar",
                amount=Decimal("61.00"),
                status=ExpenseStatus.PENDING,
            ),
            actor=ACTOR,
        )

        summary = await service.day_summary(DAY)

        assert summary.total == Decimal("161.00")
        assert summary.paid_total == Decimal("100.00")
        assert summary.pending_total == Decimal("61.00")

    async def test_the_arqueo_splits_cash_from_transfer(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        await service.create_expense(
            ExpenseCreate(
                expense_date=DAY, category_id=gas.id, concept="Gas", amount=Decimal("100.00")
            ),
            actor=ACTOR,
        )
        await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=gas.id,
                concept="Gas",
                amount=Decimal("50.00"),
                method=PaymentMethod.TRANSFER,
            ),
            actor=ACTOR,
        )

        summary = await service.day_summary(DAY)

        assert summary.cash_total == Decimal("100.00")
        assert summary.transfer_total == Decimal("50.00")

    async def test_a_day_with_nothing_on_it_is_zero(self) -> None:
        service, _, _, _ = make_service()

        summary = await service.day_summary(DAY)

        assert summary.total == Decimal("0.00")
        assert summary.count == 0
        assert summary.by_category == {}


class TestCorrections:
    async def test_an_expense_can_be_corrected(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        created = await service.create_expense(
            ExpenseCreate(category_id=gas.id, concept="Gas", amount=Decimal("160.00")),
            actor=ACTOR,
        )

        read = await service.update_expense(
            created.id, ExpenseUpdate(amount=Decimal("161.00"), concept="Gas — 2 sacos")
        )

        assert read.amount == Decimal("161.00")
        assert read.concept == "Gas — 2 sacos"

    async def test_correcting_into_a_category_out_of_use_is_refused(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        old = make_category(repository, "Fichas", active=False, order=2)
        created = await service.create_expense(
            ExpenseCreate(category_id=gas.id, concept="Gas", amount=Decimal("1.00")),
            actor=ACTOR,
        )

        with pytest.raises(ConflictError, match="no longer in use"):
            await service.update_expense(created.id, ExpenseUpdate(category_id=old.id))

    async def test_correcting_over_someone_elses_edit_is_a_version_conflict(self) -> None:
        """Plan 0004 D6: the device built this on a row that has since moved.

        Its own exception, not a plain conflict: this is the one refusal a person
        resolves by looking at both versions, so sync answers `conflict` for it
        and `rejected` for everything else.
        """
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        created = await service.create_expense(
            ExpenseCreate(category_id=gas.id, concept="Gas", amount=Decimal("160.00")),
            actor=ACTOR,
        )
        stored = await repository.get_expense(created.id)
        assert stored is not None
        stored.version = 4

        with pytest.raises(StaleVersionError):
            await service.update_expense(
                created.id, ExpenseUpdate(amount=Decimal("161.00")), base_version=1
            )

    async def test_a_correction_with_no_version_is_not_checked(self) -> None:
        """An admin editing over REST is not doing optimistic concurrency."""
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        created = await service.create_expense(
            ExpenseCreate(category_id=gas.id, concept="Gas", amount=Decimal("160.00")),
            actor=ACTOR,
        )

        read = await service.update_expense(created.id, ExpenseUpdate(amount=Decimal("161.00")))

        assert read.amount == Decimal("161.00")


class TestCategories:
    async def test_two_categories_cannot_share_a_name(self) -> None:
        service, repository, _, _ = make_service()
        make_category(repository, "Gas")

        with pytest.raises(ConflictError, match="already exists"):
            await service.create_category(ExpenseCategoryCreate(name="gas"))

    async def test_taking_a_category_out_of_use_is_not_a_tombstone(self) -> None:
        """Last month's expenses point at it; a category that vanished would
        leave them uncategorised."""
        service, repository, _, _ = make_service()
        category = make_category(repository, "Fichas de secadora")

        updated = await service.update_category(
            category.id, ExpenseCategoryUpdate(is_active=False)
        )

        assert updated.is_active is False
        assert updated.deleted_at is None


class TestTheDateLock:
    """D9, on the module where it bites hardest: every expense lands on a day's
    sheet, so all three writes consult the lock."""

    async def test_nothing_is_added_to_a_day_already_closed(self) -> None:
        service, repository, _, _ = make_service(closed=FakeClosedDays(DAY))
        gas = make_category(repository, "Gas")

        with pytest.raises(ConflictError, match="already closed"):
            await service.create_expense(
                ExpenseCreate(
                    expense_date=DAY,
                    category_id=gas.id,
                    concept="Gas — 2 sacos",
                    amount=Decimal("161.00"),
                ),
                actor=ACTOR,
            )

        assert repository.expenses == []

    async def test_what_is_already_there_is_not_corrected_either(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        expense = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY,
                category_id=gas.id,
                concept="Gas — 2 sacos",
                amount=Decimal("161.00"),
            ),
            actor=ACTOR,
        )

        service.closed_days = FakeClosedDays(DAY)

        with pytest.raises(ConflictError, match="already closed"):
            await service.update_expense(expense.id, ExpenseUpdate(amount=Decimal("200.00")))

    async def test_an_expense_cannot_be_dragged_into_a_closed_day(self) -> None:
        """The check runs on both dates. Only guarding the one it is leaving
        would let a filed day be balanced by moving an expense across the line."""
        service, repository, _, _ = make_service(closed=FakeClosedDays(DAY))
        gas = make_category(repository, "Gas")
        expense = Expense(
            expense_date=date(2026, 7, 19),
            category_id=gas.id,
            concept="Gas — 2 sacos",
            amount=Decimal("161.00"),
            method=PaymentMethod.CASH,
            status=ExpenseStatus.PAID,
            created_by_id=ACTOR_ID,
        )
        repository.add(expense)

        with pytest.raises(ConflictError, match="already closed"):
            await service.update_expense(expense.id, ExpenseUpdate(expense_date=DAY))

    async def test_nor_taken_off_a_closed_day(self) -> None:
        service, repository, _, _ = make_service()
        gas = make_category(repository, "Gas")
        expense = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY, category_id=gas.id, concept="Gas", amount=Decimal("161.00")
            ),
            actor=ACTOR,
        )

        service.closed_days = FakeClosedDays(DAY)

        with pytest.raises(ConflictError, match="already closed"):
            await service.void_expense(expense.id, ExpenseVoid(reason="Duplicado"), actor=ACTOR)

    async def test_an_open_day_is_untouched_by_a_neighbour_being_closed(self) -> None:
        service, repository, _, _ = make_service(closed=FakeClosedDays(date(2026, 7, 17)))
        gas = make_category(repository, "Gas")

        read = await service.create_expense(
            ExpenseCreate(
                expense_date=DAY, category_id=gas.id, concept="Gas", amount=Decimal("161.00")
            ),
            actor=ACTOR,
        )

        assert read.expense_date == DAY
