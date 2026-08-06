"""The day filed and locked (Plan 0005 §6.1, D9).

What is exercised here is the close as an *act*: that the preview adds up the
three modules that hold money, that filing it freezes the figures, that a day
cannot be closed twice or before it has happened, and that reopening leaves a
name behind and frees the date to be closed again.

No database: the fakes below stand in for the three services and one session.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError
from src.modules.daily_close.models import DailyClosure
from src.modules.daily_close.repository import DailyCloseRepository
from src.modules.daily_close.schemas import DailyCloseCreate, DailyCloseReopen
from src.modules.daily_close.service import DailyCloseService
from src.modules.daily_close.totals import Split
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.inventory.service import InventoryService
from src.modules.orders.models import OrderStatus, PaymentMethod
from src.modules.orders.schemas import DailySummary
from src.modules.orders.service import OrdersService

DAY = date(2026, 7, 18)
ACTOR_ID = uuid4()

CASH = PaymentMethod.CASH
TRANSFER = PaymentMethod.TRANSFER


class FakeUser:
    id = ACTOR_ID


ACTOR = cast(User, FakeUser())


def q(amount: str) -> Decimal:
    return Decimal(amount)


class FakeOrdersService:
    def __init__(
        self,
        *,
        income: Split | None = None,
        delivered: int = 0,
        by_status: dict[OrderStatus, int] | None = None,
        balance: Decimal = Decimal("0.00"),
    ) -> None:
        self.income = income or Split()
        self.delivered = delivered
        self.by_status = by_status or {}
        self.balance = balance

    async def income_on(self, day: date) -> Split:
        del day
        return self.income

    async def orders_delivered_on(self, day: date) -> int:
        del day
        return self.delivered

    async def daily_summary(self, day: date) -> DailySummary:
        return DailySummary(
            order_date=day,
            orders=sum(self.by_status.values()),
            by_status=self.by_status,
            pieces=0,
            subtotal=q("0.00"),
            discount_total=q("0.00"),
            total=q("0.00"),
            collected=q("0.00"),
            balance=self.balance,
        )


class FakeInventoryService:
    def __init__(self, income: Split | None = None) -> None:
        self.income = income or Split()

    async def supplies_income(self, on_date: date) -> Split:
        del on_date
        return self.income


class FakeExpensesService:
    def __init__(
        self, *, total: Decimal = Decimal("0.00"), paid: Split | None = None
    ) -> None:
        self.total = total
        self.paid = paid or Split()

    async def expenses_total(self, on_date: date) -> Decimal:
        del on_date
        return self.total

    async def paid_expenses(self, on_date: date) -> Split:
        del on_date
        return self.paid


class FakeDailyCloseRepository:
    def __init__(self) -> None:
        self.closures: list[DailyClosure] = []
        self.commits = 0

    async def get(self, closure_id: UUID) -> DailyClosure | None:
        return next(
            (c for c in self.closures if c.id == closure_id and c.deleted_at is None), None
        )

    async def get_for_date(self, day: date) -> DailyClosure | None:
        return next(
            (c for c in self.closures if c.close_date == day and c.deleted_at is None), None
        )

    async def is_closed(self, day: date) -> bool:
        return await self.get_for_date(day) is not None

    async def list_between(
        self, *, since: date, until: date, include_reopened: bool = False
    ) -> list[DailyClosure]:
        return [
            closure
            for closure in self.closures
            if since <= closure.close_date <= until
            and (include_reopened or closure.deleted_at is None)
        ]

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = uuid4()
        if getattr(instance, "version", None) is None:
            instance.version = 1
        self.closures.append(instance)

    async def commit(self) -> None:
        self.commits += 1


def make_service(
    *,
    orders: FakeOrdersService | None = None,
    inventory: FakeInventoryService | None = None,
    expenses: FakeExpensesService | None = None,
) -> tuple[DailyCloseService, FakeDailyCloseRepository]:
    repository = FakeDailyCloseRepository()
    service = DailyCloseService(
        cast(DailyCloseRepository, repository),
        cast(OrdersService, orders or FakeOrdersService()),
        cast(InventoryService, inventory or FakeInventoryService()),
        cast(ExpensesService, expenses or FakeExpensesService()),
    )
    return service, repository


def the_real_day() -> dict[str, Any]:
    """The sheet of 18/07/26: Q855 + Q140 in, Q231 out, Q50 by transfer."""
    return {
        "orders": FakeOrdersService(
            income=Split.of([(q("805.00"), CASH), (q("50.00"), TRANSFER)]),
            delivered=11,
        ),
        "inventory": FakeInventoryService(Split.of([(q("140.00"), CASH)])),
        "expenses": FakeExpensesService(
            total=q("231.00"), paid=Split.of([(q("231.00"), CASH)])
        ),
    }


class TestThePreview:
    async def test_it_adds_up_the_three_modules_that_hold_money(self) -> None:
        service, _ = make_service(**the_real_day())

        preview = await service.preview(DAY)

        assert preview.orders_income == q("855.00")
        assert preview.supplies_income == q("140.00")
        assert preview.income_total == q("995.00")
        assert preview.expenses_total == q("231.00")
        assert preview.net_total == q("764.00")
        assert preview.orders_delivered == 11

    async def test_it_carries_the_arqueo(self) -> None:
        service, _ = make_service(**the_real_day())

        preview = await service.preview(DAY)

        assert preview.cash_income == q("945.00")
        assert preview.transfer_income == q("50.00")
        assert preview.cash_on_hand == q("714.00")

    async def test_an_open_day_is_not_closed_and_names_no_record(self) -> None:
        service, _ = make_service(**the_real_day())

        preview = await service.preview(DAY)

        assert preview.is_closed is False
        assert preview.closure_id is None

    async def test_once_filed_it_points_at_the_record(self) -> None:
        service, _ = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        preview = await service.preview(DAY)

        assert preview.is_closed is True
        assert preview.closure_id == closure.id

    async def test_a_quiet_day_previews_at_zero_rather_than_failing(self) -> None:
        service, _ = make_service()

        preview = await service.preview(DAY)

        assert preview.income_total == q("0.00")
        assert preview.net_total == q("0.00")
        assert preview.warnings == []


class TestWarnings:
    async def test_tickets_still_in_the_shop_are_worth_saying_out_loud(self) -> None:
        service, _ = make_service(
            orders=FakeOrdersService(
                by_status={OrderStatus.READY: 2, OrderStatus.DELIVERED: 5}
            )
        )

        preview = await service.preview(DAY)

        assert any("have not been handed back" in w for w in preview.warnings)

    async def test_a_delivered_day_says_nothing_about_deliveries(self) -> None:
        service, _ = make_service(
            orders=FakeOrdersService(by_status={OrderStatus.DELIVERED: 7})
        )

        preview = await service.preview(DAY)

        assert not any("handed back" in w for w in preview.warnings)

    async def test_money_still_owed_on_the_day_is_flagged(self) -> None:
        service, _ = make_service(orders=FakeOrdersService(balance=q("120.00")))

        preview = await service.preview(DAY)

        assert any("uncollected" in w for w in preview.warnings)

    async def test_expenses_left_pending_are_flagged(self) -> None:
        """Q200 written down, Q120 actually paid: Q80 the drawer never saw."""
        service, _ = make_service(
            expenses=FakeExpensesService(
                total=q("200.00"), paid=Split.of([(q("120.00"), CASH)])
            )
        )

        preview = await service.preview(DAY)

        assert any("80.00" in w and "pending" in w for w in preview.warnings)

    async def test_none_of_them_stops_the_close(self) -> None:
        """§6.1: a warning, never a block. The counter is standing there with
        the drawer open, and a close that refused would just be worked around."""
        service, _ = make_service(
            orders=FakeOrdersService(by_status={OrderStatus.READY: 3}, balance=q("50.00"))
        )

        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        assert closure.close_date == DAY


class TestClosing:
    async def test_it_files_the_figures_the_preview_showed(self) -> None:
        service, repository = make_service(**the_real_day())

        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        assert closure.orders_income == q("855.00")
        assert closure.supplies_income == q("140.00")
        assert closure.net_total == q("764.00")
        assert closure.cash_on_hand == q("714.00")
        assert closure.orders_delivered == 11
        assert closure.closed_by_id == ACTOR_ID
        assert len(repository.closures) == 1

    async def test_it_keeps_the_note_whoever_closed_left(self) -> None:
        service, _ = make_service(**the_real_day())

        closure = await service.close(
            DailyCloseCreate(close_date=DAY, notes="Faltó Q5 en caja"), actor=ACTOR
        )

        assert closure.notes == "Faltó Q5 en caja"

    async def test_without_a_date_it_closes_today(self) -> None:
        service, _ = make_service()

        closure = await service.close(DailyCloseCreate(), actor=ACTOR)

        assert closure.close_date == business_date()

    async def test_a_day_is_closed_once(self) -> None:
        service, _ = make_service(**the_real_day())
        await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        with pytest.raises(ConflictError, match="already closed"):
            await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

    async def test_a_day_that_has_not_happened_cannot_be_closed(self) -> None:
        """Closing tomorrow would file an acta for hours nobody has worked."""
        service, _ = make_service()
        tomorrow = business_date() + timedelta(days=1)

        with pytest.raises(ConflictError, match="has not happened"):
            await service.close(DailyCloseCreate(close_date=tomorrow), actor=ACTOR)

    async def test_a_past_day_can_still_be_closed(self) -> None:
        """The counter forgot on Saturday and does it on Monday."""
        service, _ = make_service(**the_real_day())

        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        assert closure.close_date == DAY

    async def test_the_figures_stop_moving_once_filed(self) -> None:
        """The point of a snapshot (D9): what the sources say afterwards does not
        rewrite the acta."""
        expenses = FakeExpensesService(total=q("231.00"), paid=Split.of([(q("231.00"), CASH)]))
        service, _ = make_service(**{**the_real_day(), "expenses": expenses})
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        expenses.total = q("999.00")

        assert closure.expenses_total == q("231.00")
        assert closure.net_total == q("764.00")


class TestTheLock:
    async def test_a_closed_day_answers_yes(self) -> None:
        """What `orders`, `expenses` and `supply_sales` ask before writing."""
        service, repository = make_service(**the_real_day())
        await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        assert await repository.is_closed(DAY) is True
        assert await repository.is_closed(DAY - timedelta(days=1)) is False

    async def test_reopening_lifts_it(self) -> None:
        service, repository = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        await service.reopen(closure.id, DailyCloseReopen(reason="Faltó un gasto"), actor=ACTOR)

        assert await repository.is_closed(DAY) is False


class TestReopening:
    async def test_it_leaves_a_name_and_a_reason(self) -> None:
        service, _ = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        reopened = await service.reopen(
            closure.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR
        )

        assert reopened.reopened_by_id == ACTOR_ID
        assert reopened.reopen_reason == "Faltó el gas"

    async def test_the_row_stays_on_the_record(self) -> None:
        """A day closed at Q764 and then reopened is a fact, not a gap."""
        service, repository = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        await service.reopen(closure.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR)

        assert len(repository.closures) == 1
        assert repository.closures[0].net_total == q("764.00")

    async def test_the_date_can_be_closed_again(self) -> None:
        service, repository = make_service(**the_real_day())
        first = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)
        await service.reopen(first.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR)

        second = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        assert second.id != first.id
        assert len(repository.closures) == 2

    async def test_until_it_is_the_preview_says_so(self) -> None:
        """D9 says a reopening obliges a re-close. Nothing can force a person's
        hand, so the day says it out loud until somebody does."""
        service, _ = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)
        await service.reopen(closure.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR)

        preview = await service.preview(DAY)

        assert preview.is_closed is False
        assert any("has to be closed again" in w for w in preview.warnings)

    async def test_reopening_the_same_close_twice_is_not_found(self) -> None:
        service, _ = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)
        await service.reopen(closure.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR)

        with pytest.raises(NotFoundError):
            await service.reopen(
                closure.id, DailyCloseReopen(reason="Otra vez"), actor=ACTOR
            )

    async def test_reopening_something_that_never_existed_is_not_found(self) -> None:
        service, _ = make_service()

        with pytest.raises(NotFoundError):
            await service.reopen(uuid4(), DailyCloseReopen(reason="Nada"), actor=ACTOR)


class TestHistory:
    async def test_it_shows_the_closes_in_the_range(self) -> None:
        service, _ = make_service(**the_real_day())
        await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)
        await service.close(DailyCloseCreate(close_date=DAY - timedelta(days=1)), actor=ACTOR)

        history = await service.history(since=DAY - timedelta(days=7), until=DAY)

        assert len(history) == 2

    async def test_it_leaves_out_what_falls_outside(self) -> None:
        service, _ = make_service(**the_real_day())
        await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)

        history = await service.history(
            since=DAY - timedelta(days=7), until=DAY - timedelta(days=1)
        )

        assert history == []

    async def test_reopened_days_are_part_of_the_trail(self) -> None:
        service, _ = make_service(**the_real_day())
        closure = await service.close(DailyCloseCreate(close_date=DAY), actor=ACTOR)
        await service.reopen(closure.id, DailyCloseReopen(reason="Faltó el gas"), actor=ACTOR)

        history = await service.history(since=DAY, until=DAY)

        assert len(history) == 1
        assert history[0].reopen_reason == "Faltó el gas"

    async def test_a_range_that_ends_before_it_starts_is_refused(self) -> None:
        service, _ = make_service()

        with pytest.raises(ConflictError):
            await service.history(since=DAY, until=DAY - timedelta(days=1))
