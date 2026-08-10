"""Importing a day off the «Registro Diario» sheet (Plan 0005 §1).

This is the only reading in the system that ends in money moving, so what is
under test is mostly the *refusals*: the amount that gets clamped to the balance,
the ticket that is already settled, the row that fails without taking the other
fourteen with it, and the sheet that cannot be imported twice.

No provider and no database. The extractor is a stub returning a reading and the
services are fakes that answer the way the real ones do — including their
conflicts, because "that payment is already registered" is not an error here, it
is the idempotency working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.expenses.models import ExpenseStatus
from src.modules.intake_scan.cash_normalization import (
    decode_mark,
    match_category,
    match_employee,
    parse_clock,
    resolve_sheet_date,
    resolve_shift,
)
from src.modules.intake_scan.cash_schemas import (
    EXPENSE_MATCHED,
    EXPENSE_NO_CATEGORY,
    INCOME_AMOUNT_MISMATCH,
    INCOME_CANCELLED,
    INCOME_MATCHED,
    INCOME_NOT_FOUND,
    INCOME_SETTLED,
    INCOME_SUPPLY,
    CashExpenseApply,
    CashIncomeApply,
    CashSheetApply,
)
from src.modules.intake_scan.cash_service import CashSheetService, row_id
from src.modules.intake_scan.models import ScanJob, ScanPurpose, ScanStatus
from src.modules.orders.models import OrderStatus, PaymentMethod
from src.modules.orders.repository import serial_key

TODAY = date(2026, 8, 9)


def leaf(value: Any, confidence: float = 0.95, raw: str | None = None) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "raw_text": raw}


# ------------------------------------------------------------------ los dobles


@dataclass
class FakeOrder:
    id: UUID = field(default_factory=uuid4)
    order_date: date = TODAY
    daily_number: int = 939
    booklet_serial: str | None = "939"
    status: OrderStatus = OrderStatus.RECEIVED
    total: Decimal = Decimal("115.00")
    balance: Decimal = Decimal("115.00")


@dataclass
class FakeCategory:
    id: UUID = field(default_factory=uuid4)
    name: str = "Gas"
    is_active: bool = True


@dataclass
class FakeEmployee:
    id: UUID = field(default_factory=uuid4)
    full_name: str = "María López"
    is_active: bool = True


class FakeOrdersRepository:
    def __init__(self, orders: list[FakeOrder] | None = None) -> None:
        self.orders = orders or []

    async def find_by_serials(self, serials: Any) -> dict[str, list[FakeOrder]]:
        """Grouped by `serial_key`, the way the real query's `case` does it."""
        wanted = {serial for serial in serials if serial}
        found: dict[str, list[FakeOrder]] = {}
        for order in self.orders:
            key = serial_key(order.booklet_serial)
            if key is not None and key in wanted:
                found.setdefault(key, []).append(order)
        return found


class FakeOrdersService:
    """Answers like `OrdersService`, refusals included."""

    def __init__(self) -> None:
        self.payments: list[tuple[UUID, Decimal, PaymentMethod, UUID | None]] = []
        self.delivered: list[UUID] = []
        self.seen_ids: set[UUID] = set()
        self.orders: dict[UUID, FakeOrder] = {}

    def _take(self, order_id: UUID, payment: Any) -> None:
        order = self.orders.get(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        if payment.id is not None and payment.id in self.seen_ids:
            raise ConflictError("That payment is already registered.")
        if payment.amount > order.balance:
            raise ConflictError(f"The payment is larger than the balance of {order.balance}.")
        self.seen_ids.add(payment.id)
        order.balance -= payment.amount
        self.payments.append((order_id, payment.amount, payment.method, payment.id))

    async def add_payment(self, order_id: UUID, data: Any, *, actor: Any) -> Any:
        self._take(order_id, data)

    async def deliver(self, order_id: UUID, data: Any, *, actor: Any) -> Any:
        order = self.orders.get(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        if order.status is OrderStatus.DELIVERED:
            raise ConflictError("An order that is 'delivered' can no longer be delivered.")
        if data.payment is not None:
            self._take(order_id, data.payment)
        order.status = OrderStatus.DELIVERED
        self.delivered.append(order_id)


class FakeExpensesService:
    def __init__(self, categories: list[FakeCategory] | None = None) -> None:
        self.categories = categories or []
        self.created: list[Any] = []
        self.seen_ids: set[UUID] = set()

    async def list_categories(self) -> list[FakeCategory]:
        return self.categories

    async def create_expense(self, data: Any, *, actor: Any) -> Any:
        if data.id is not None and data.id in self.seen_ids:
            raise ConflictError("That expense is already registered.")
        self.seen_ids.add(data.id)
        self.created.append(data)


class FakeStaffService:
    def __init__(self, employees: list[FakeEmployee] | None = None) -> None:
        self.employees = employees or []
        self.records: list[Any] = []

    async def list_employees(self) -> list[FakeEmployee]:
        return self.employees

    async def clock_in(self, data: Any) -> Any:
        self.records.append(data)


class FakeScanRepository:
    def __init__(self, job: ScanJob | None = None) -> None:
        self.jobs: list[ScanJob] = [job] if job else []
        self.commits = 0

    def add(self, job: ScanJob) -> None:
        self.jobs.append(job)

    async def get(self, scan_id: UUID) -> ScanJob | None:
        return next((job for job in self.jobs if job.id == scan_id), None)

    async def flush(self) -> None: ...

    async def commit(self) -> None:
        self.commits += 1

    async def count_today(self) -> int:
        return 0


class FakeClosedDays:
    def __init__(self, closed: set[date] | None = None) -> None:
        self.closed = closed or set()

    async def is_closed(self, day: date) -> bool:
        return day in self.closed


def build(
    *,
    orders: list[FakeOrder] | None = None,
    categories: list[FakeCategory] | None = None,
    employees: list[FakeEmployee] | None = None,
    job: ScanJob | None = None,
    closed: set[date] | None = None,
) -> tuple[CashSheetService, dict[str, Any]]:
    doubles = {
        "scans": FakeScanRepository(job),
        "orders_repo": FakeOrdersRepository(orders),
        "orders": FakeOrdersService(),
        "expenses": FakeExpensesService(categories),
        "staff": FakeStaffService(employees),
        "closed": FakeClosedDays(closed),
    }
    for order in orders or []:
        doubles["orders"].orders[order.id] = order

    service = CashSheetService(
        doubles["scans"],  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        doubles["orders_repo"],  # type: ignore[arg-type]
        doubles["orders"],  # type: ignore[arg-type]
        doubles["expenses"],  # type: ignore[arg-type]
        doubles["staff"],  # type: ignore[arg-type]
        doubles["closed"],  # type: ignore[arg-type]
    )
    return service, doubles


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.modules.intake_scan.cash_service.business_date", lambda: TODAY
    )


# ------------------------------------------------------------------- la lectura


class TestReadingTheDate:
    def test_two_digit_year_is_this_century(self) -> None:
        parsed, _, warnings = resolve_sheet_date(4, 8, 26, today=TODAY)
        assert parsed == date(2026, 8, 4)
        assert warnings == ["sheet_not_today:2026-08-04"]

    def test_todays_sheet_says_nothing(self) -> None:
        parsed, _, warnings = resolve_sheet_date(9, 8, 2026, today=TODAY)
        assert parsed == TODAY
        assert warnings == []

    def test_a_blank_date_falls_back_to_today(self) -> None:
        """The person is holding the sheet; the screen puts the date on top."""
        parsed, confidence, warnings = resolve_sheet_date(None, None, None, today=TODAY)
        assert (parsed, confidence, warnings) == (TODAY, 0.0, ["date_unreadable"])

    def test_the_thirty_first_of_february_is_a_misread_digit(self) -> None:
        parsed, _, warnings = resolve_sheet_date(31, 2, 26, today=TODAY)
        assert parsed == TODAY
        assert warnings == ["date_unreadable:2026-2-31"]


class TestTheHighlighter:
    """The colour is the only thing that says cash from transfer (Plan 0005 §1)."""

    @pytest.mark.parametrize(
        ("mark", "method", "invoice", "supply"),
        [
            ("transfer", PaymentMethod.TRANSFER, False, False),
            ("invoice", PaymentMethod.CASH, True, False),
            ("supply", PaymentMethod.CASH, False, True),
            ("none", PaymentMethod.CASH, False, False),
            (None, PaymentMethod.CASH, False, False),
            ("verde fosforescente", PaymentMethod.CASH, False, False),
        ],
    )
    def test_decoding(
        self, mark: str | None, method: PaymentMethod, invoice: bool, supply: bool
    ) -> None:
        assert decode_mark(mark) == (method, invoice, supply)


class TestReadingTheClock:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("7:00", time(7, 0)),
            ("7.15", time(7, 15)),
            ("12.40", time(12, 40)),
            ("6:40", time(6, 40)),
            ("700", time(7, 0)),
            ("—1—", None),
            ("/", None),
            ("", None),
            (None, None),
            ("25:00", None),
            ("7:75", None),
        ],
    )
    def test_the_ways_a_time_gets_written(
        self, written: str | None, expected: time | None
    ) -> None:
        assert parse_clock(written) == expected

    def test_an_evening_written_as_a_morning_is_moved(self) -> None:
        """A day that starts at 7:15 does not end at 6:40 in the morning."""
        clock_out, adjusted = resolve_shift(time(7, 15), time(6, 40))
        assert (clock_out, adjusted) == (time(18, 40), True)

    def test_an_ordinary_day_is_left_alone(self) -> None:
        clock_out, adjusted = resolve_shift(time(7, 15), time(12, 40))
        assert (clock_out, adjusted) == (time(12, 40), False)


class TestMatchingTheCategory:
    def test_the_written_name_wins(self) -> None:
        gas, insumos = FakeCategory(name="Gas"), FakeCategory(name="Compra de insumos")
        category, how = match_category("gas", [gas, insumos])  # type: ignore[arg-type]
        assert (category, how) == (gas, "exact")

    def test_the_sheets_vocabulary_points_at_a_category(self) -> None:
        insumos = FakeCategory(name="Compra de insumos")
        category, how = match_category("18 detergente", [insumos])  # type: ignore[arg-type]
        assert (category, how) == (insumos, "keyword")

    def test_a_word_inside_another_word_is_not_a_match(self) -> None:
        """«gas» inside «gastos» is not a purchase of gas."""
        gas = FakeCategory(name="Gas")
        assert match_category("gastos varios", [gas]) == (None, None)  # type: ignore[arg-type]

    def test_an_inactive_category_is_never_proposed(self) -> None:
        gas = FakeCategory(name="Gas", is_active=False)
        assert match_category("gas", [gas]) == (None, None)  # type: ignore[arg-type]


class TestMatchingTheEmployee:
    def test_a_first_name_finds_the_one_person_it_can_be(self) -> None:
        maria = FakeEmployee(full_name="María López")
        assert match_employee("maria", [maria]) is maria  # type: ignore[arg-type]

    def test_two_marias_is_a_question_for_a_person(self) -> None:
        staff = [FakeEmployee(full_name="María López"), FakeEmployee(full_name="María Chen")]
        assert match_employee("maria", staff) is None  # type: ignore[arg-type]


# -------------------------------------------------------------- las filas


def sheet(
    *,
    incomes: list[dict[str, Any]] | None = None,
    expenses: list[dict[str, Any]] | None = None,
    attendance: list[dict[str, Any]] | None = None,
    totals: dict[str, Any] | None = None,
    on: date = TODAY,
) -> dict[str, Any]:
    return {
        "blocks": [
            {
                "date": {
                    "day": leaf(on.day),
                    "month": leaf(on.month),
                    "year": leaf(on.year % 100),
                },
                "incomes": incomes or [],
                "expenses": expenses or [],
                "attendance": attendance or [],
                "totals": totals or {},
            }
        ]
    }


def income(serial: str | None, amount: Any, mark: str = "none") -> dict[str, Any]:
    return {
        "booklet_serial": leaf(serial),
        "customer_text": leaf("Elmer González"),
        "amount": leaf(amount),
        "mark": leaf(mark),
    }


async def draft_of(service: CashSheetService, payload: dict[str, Any]) -> Any:
    from src.modules.intake_scan.cash_schemas import RawCashSheet

    draft, _ = await service.build_draft(RawCashSheet.model_validate(payload))
    return draft.days[0]


class TestMatchingAnIncomeRow:
    async def test_a_ticket_that_owes_what_the_paper_says(self) -> None:
        """The booklet prints `Nº 000939`; the sheet writes `939`. One ticket."""
        order = FakeOrder(booklet_serial="000939", balance=Decimal("115.00"))
        service, _ = build(orders=[order])

        day = await draft_of(service, sheet(incomes=[income("939", 115)]))

        row = day.incomes[0]
        assert row.status == INCOME_MATCHED
        assert row.order_id == order.id
        assert row.amount_suggested == Decimal("115.00")
        assert row.can_deliver is True

    @pytest.mark.parametrize(
        ("written", "stored"),
        [("939", "000939"), ("000939", "939"), ("#939", "000939"), ("939", "939")],
    )
    def test_padding_never_makes_two_tickets(self, written: str, stored: str) -> None:
        assert serial_key(written) == serial_key(stored)

    def test_letters_keep_every_character(self) -> None:
        """Nothing says the zeros in `A-0042` are padding."""
        assert serial_key("A-0042") != serial_key("A-42")

    async def test_a_number_no_ticket_carries(self) -> None:
        service, _ = build(orders=[])
        day = await draft_of(service, sheet(incomes=[income("939", 115)]))
        assert day.incomes[0].status == INCOME_NOT_FOUND
        assert day.incomes[0].amount_suggested is None

    async def test_a_ticket_that_owes_nothing_is_not_collected_again(self) -> None:
        order = FakeOrder(balance=Decimal("0.00"), status=OrderStatus.READY)
        service, _ = build(orders=[order])

        row = (await draft_of(service, sheet(incomes=[income("939", 115)]))).incomes[0]

        assert row.status == INCOME_SETTLED
        assert row.amount_suggested is None
        # Paid in advance, still in the shop: the sheet is recording the delivery.
        assert row.can_deliver is True

    async def test_more_on_the_paper_than_the_ticket_owes_is_clamped(self) -> None:
        """Change handed back at the counter is not income (Plan 0001 §6.1)."""
        order = FakeOrder(balance=Decimal("115.00"))
        service, _ = build(orders=[order])

        row = (await draft_of(service, sheet(incomes=[income("939", 150)]))).incomes[0]

        assert row.status == INCOME_AMOUNT_MISMATCH
        assert row.amount_read.value == Decimal("150")
        assert row.amount_suggested == Decimal("115.00")

    async def test_a_voided_ticket_takes_no_money(self) -> None:
        order = FakeOrder(status=OrderStatus.CANCELLED)
        service, _ = build(orders=[order])

        row = (await draft_of(service, sheet(incomes=[income("939", 115)]))).incomes[0]

        assert row.status == INCOME_CANCELLED
        assert row.amount_suggested is None

    async def test_a_pink_row_is_a_transfer(self) -> None:
        service, _ = build(orders=[FakeOrder()])
        row = (
            await draft_of(service, sheet(incomes=[income("939", 115, "transfer")]))
        ).incomes[0]
        assert row.method is PaymentMethod.TRANSFER

    async def test_a_blue_row_is_a_supply_and_names_no_ticket(self) -> None:
        service, _ = build(orders=[FakeOrder()])
        row = (
            await draft_of(service, sheet(incomes=[income(None, 90, "supply")]))
        ).incomes[0]
        assert row.status == INCOME_SUPPLY
        assert row.order_id is None

    async def test_two_tickets_under_one_serial_are_reported(self) -> None:
        service, _ = build(
            orders=[FakeOrder(booklet_serial="939"), FakeOrder(booklet_serial="939")]
        )
        day = await draft_of(service, sheet(incomes=[income("939", 115)]))
        assert "duplicate_serial:939" in day.warnings


class TestMatchingAnExpenseRow:
    async def test_the_two_columns_read_as_one_concept(self) -> None:
        service, _ = build(categories=[FakeCategory(name="Compra de insumos")])

        day = await draft_of(
            service,
            sheet(
                expenses=[
                    {
                        "object_text": leaf("18"),
                        "description": leaf("detergente"),
                        "amount": leaf(10),
                        "observations": leaf(None, 0.0),
                    }
                ]
            ),
        )

        row = day.expenses[0]
        assert row.status == EXPENSE_MATCHED
        assert row.concept.value == "detergente 18"
        assert row.category_name == "Compra de insumos"
        assert row.expense_status is ExpenseStatus.PAID

    async def test_words_nothing_matches_still_come_back_to_be_filed(self) -> None:
        service, _ = build(categories=[FakeCategory(name="Gas")])

        day = await draft_of(
            service,
            sheet(
                expenses=[
                    {
                        "object_text": leaf(None, 0.0),
                        "description": leaf("cuerda de nylon"),
                        "amount": leaf(35),
                        "observations": leaf(None, 0.0),
                    }
                ]
            ),
        )

        row = day.expenses[0]
        assert row.status == EXPENSE_NO_CATEGORY
        assert row.amount.value == Decimal("35")
        assert row.category_id is None

    async def test_the_notes_say_the_money_has_not_left_yet(self) -> None:
        service, _ = build(categories=[FakeCategory(name="Gas")])

        day = await draft_of(
            service,
            sheet(
                expenses=[
                    {
                        "object_text": leaf(None, 0.0),
                        "description": leaf("gas"),
                        "amount": leaf(160),
                        "observations": leaf("pago atrasado"),
                    }
                ]
            ),
        )

        assert day.expenses[0].expense_status is ExpenseStatus.PENDING

    async def test_overtime_is_filed_against_the_person_it_paid(self) -> None:
        maria = FakeEmployee(full_name="María López")
        service, _ = build(
            categories=[FakeCategory(name="Horas extra")], employees=[maria]
        )

        day = await draft_of(
            service,
            sheet(
                expenses=[
                    {
                        "object_text": leaf("María"),
                        "description": leaf("extra"),
                        "amount": leaf(20),
                        "observations": leaf(None, 0.0),
                    }
                ]
            ),
        )

        assert day.expenses[0].employee_id == maria.id


class TestCrossCheckingTheSums:
    async def test_a_column_that_does_not_add_up_is_reported(self) -> None:
        service, _ = build(orders=[FakeOrder(booklet_serial="939")])

        day = await draft_of(
            service,
            sheet(
                incomes=[income("939", 115)],
                totals={"income_total": leaf(1130)},
            ),
        )

        assert "income_sum_mismatch:1130:115" in day.warnings

    async def test_a_closed_day_is_said_before_anything_is_confirmed(self) -> None:
        service, _ = build(closed={TODAY})
        day = await draft_of(service, sheet())
        assert f"day_closed:{TODAY.isoformat()}" in day.warnings


# ------------------------------------------------------------------- aplicar


def applied_job(scan_id: UUID) -> ScanJob:
    return ScanJob(
        id=scan_id,
        status=ScanStatus.COMPLETED,
        purpose=ScanPurpose.CASH_CLOSE,
        image_path="2026/08/09/x.jpg",
        model="gemini-2.5-flash",
        prompt_version="cash_v1",
        created_by_id=uuid4(),
    )


class TestApplying:
    async def test_a_confirmed_row_collects_and_delivers(self) -> None:
        scan_id = uuid4()
        order = FakeOrder(balance=Decimal("115.00"))
        service, doubles = build(orders=[order], job=applied_job(scan_id))

        result = await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                incomes=[
                    CashIncomeApply(index=0, order_id=order.id, amount=Decimal("115.00"))
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert result.payments_applied == 1
        assert result.orders_delivered == 1
        assert result.collected_total == Decimal("115.00")
        assert doubles["orders"].delivered == [order.id]

    async def test_paying_without_delivering_leaves_the_clothes(self) -> None:
        scan_id = uuid4()
        order = FakeOrder(balance=Decimal("50.00"))
        service, doubles = build(orders=[order], job=applied_job(scan_id))

        await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                incomes=[
                    CashIncomeApply(
                        index=0, order_id=order.id, amount=Decimal("50.00"), deliver=False
                    )
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert doubles["orders"].delivered == []
        assert len(doubles["orders"].payments) == 1

    async def test_one_bad_row_does_not_undo_the_good_ones(self) -> None:
        """The whole reason each row is its own attempt."""
        scan_id = uuid4()
        good, gone = FakeOrder(), FakeOrder()
        service, doubles = build(orders=[good], job=applied_job(scan_id))

        result = await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                incomes=[
                    CashIncomeApply(index=0, order_id=gone.id, amount=Decimal("10.00")),
                    CashIncomeApply(index=1, order_id=good.id, amount=Decimal("115.00")),
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert result.payments_applied == 1
        assert [row.outcome for row in result.incomes] == ["failed", "applied"]
        assert doubles["orders"].delivered == [good.id]

    async def test_more_than_the_balance_is_refused_by_the_orders_service(self) -> None:
        scan_id = uuid4()
        order = FakeOrder(balance=Decimal("100.00"))
        service, _ = build(orders=[order], job=applied_job(scan_id))

        result = await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                incomes=[
                    CashIncomeApply(index=0, order_id=order.id, amount=Decimal("150.00"))
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert result.incomes[0].outcome == "failed"
        assert "larger than the balance" in (result.incomes[0].reason or "")

    async def test_expenses_are_written_with_ids_derived_from_the_scan(self) -> None:
        """A retry has to land on its own first attempt, not beside it."""
        scan_id = uuid4()
        category = FakeCategory()
        service, doubles = build(categories=[category], job=applied_job(scan_id))

        await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                expenses=[
                    CashExpenseApply(
                        index=3,
                        category_id=category.id,
                        concept="gas",
                        amount=Decimal("160.00"),
                    )
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert doubles["expenses"].created[0].id == row_id(scan_id, "expense", 3)

    async def test_a_row_that_is_already_registered_is_skipped_not_failed(self) -> None:
        scan_id = uuid4()
        category = FakeCategory()
        service, doubles = build(categories=[category], job=applied_job(scan_id))
        doubles["expenses"].seen_ids.add(row_id(scan_id, "expense", 0))

        result = await service.apply(
            scan_id,
            CashSheetApply(
                close_date=TODAY,
                expenses=[
                    CashExpenseApply(
                        index=0,
                        category_id=category.id,
                        concept="gas",
                        amount=Decimal("160.00"),
                    )
                ],
            ),
            actor=object(),  # type: ignore[arg-type]
        )

        assert result.expenses[0].outcome == "skipped"
        assert result.expenses_created == 0

    async def test_a_sheet_cannot_be_imported_twice(self) -> None:
        scan_id = uuid4()
        order = FakeOrder()
        service, _ = build(orders=[order], job=applied_job(scan_id))
        body = CashSheetApply(
            close_date=TODAY,
            incomes=[CashIncomeApply(index=0, order_id=order.id, amount=Decimal("115.00"))],
        )

        await service.apply(scan_id, body, actor=object())  # type: ignore[arg-type]

        with pytest.raises(ConflictError, match="already imported"):
            await service.apply(scan_id, body, actor=object())  # type: ignore[arg-type]

    async def test_an_imported_sheet_says_so_when_read_again(self) -> None:
        scan_id = uuid4()
        service, _ = build(job=applied_job(scan_id))
        await service.apply(
            scan_id, CashSheetApply(close_date=TODAY), actor=object()  # type: ignore[arg-type]
        )

        read = await service.get(scan_id)

        assert any(warning.startswith("already_applied:") for warning in read.warnings)

    async def test_a_ticket_scan_is_not_a_sheet(self) -> None:
        scan_id = uuid4()
        job = applied_job(scan_id)
        job.purpose = ScanPurpose.INTAKE
        service, _ = build(job=job)

        with pytest.raises(ConflictError, match="not a daily sheet"):
            await service.apply(
                scan_id,
                CashSheetApply(close_date=TODAY),
                actor=object(),  # type: ignore[arg-type]
            )
