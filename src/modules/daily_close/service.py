from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError
from src.modules.daily_close.models import DailyClosure
from src.modules.daily_close.repository import DailyCloseRepository
from src.modules.daily_close.schemas import (
    DailyCloseCreate,
    DailyClosePreview,
    DailyCloseReopen,
    DailyClosureRead,
)
from src.modules.daily_close.totals import DayTotals, add_up
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.inventory.service import InventoryService
from src.modules.orders.models import OrderStatus
from src.modules.orders.service import OrdersService

ZERO = Decimal("0.00")

#: Tickets that are still the shop's problem at closing time.
OPEN_STATUSES = frozenset(
    {OrderStatus.RECEIVED, OrderStatus.IN_PROGRESS, OrderStatus.READY}
)


class DailyCloseService:
    """The day's account: the paper sheet on screen, and then filed (§6.1, D9).

    It owns no money of its own. Each of the three modules that do answers what
    its day was worth — collected payments, sales that stand, expenses incurred —
    and this one only adds them up, writes down the answer and locks the date.
    """

    def __init__(
        self,
        repository: DailyCloseRepository,
        orders: OrdersService,
        inventory: InventoryService,
        expenses: ExpensesService,
    ) -> None:
        self.repository = repository
        self.orders = orders
        self.inventory = inventory
        self.expenses = expenses

    # -- the sheet on screen -----------------------------------------------

    async def preview(self, day: date) -> DailyClosePreview:
        """The day as it stands right now (§7 `GET /daily-close/preview`).

        Recomputed on every call, even after the day is closed: the figures
        cannot move behind a lock, so the preview and the filed acta agreeing is
        a fact worth being able to see rather than an assumption.
        """
        totals, delivered = await self._figures(day)
        closure = await self.repository.get_for_date(day)
        return DailyClosePreview(
            **self._fields(day, totals, delivered),
            warnings=await self._warnings(day, totals),
            is_closed=closure is not None,
            closure_id=closure.id if closure is not None else None,
        )

    async def _figures(self, day: date) -> tuple[DayTotals, int]:
        totals = add_up(
            orders=await self.orders.income_on(day),
            supplies=await self.inventory.supplies_income(day),
            expenses=await self.expenses.expenses_total(day),
            paid_expenses=await self.expenses.paid_expenses(day),
        )
        return totals, await self.orders.orders_delivered_on(day)

    async def _warnings(self, day: date, totals: DayTotals) -> list[str]:
        """What is worth reading before closing — never a reason to refuse (§6.1).

        The counter is standing at the end of a shift with the drawer open. A
        close that refused over an undelivered ticket would simply be worked
        around; one that says so out loud gets the ticket looked at.
        """
        warnings: list[str] = []

        summary = await self.orders.daily_summary(day)
        open_tickets = sum(
            count for status, count in summary.by_status.items() if status in OPEN_STATUSES
        )
        if open_tickets:
            warnings.append(
                f"{open_tickets} of the day's tickets have not been handed back yet."
            )
        if summary.balance > ZERO:
            warnings.append(
                f"The day's tickets still have {summary.balance} uncollected."
            )

        # `expenses_total` counts what the day owes and the arqueo only what left,
        # so the gap between them is exactly what is still pending (§5.3).
        pending = totals.expenses_total - (totals.cash_expenses + totals.transfer_expenses)
        if pending > ZERO:
            warnings.append(f"{pending} of the day's expenses are still pending payment.")

        if await self._was_reopened(day):
            # D9 says a reopening "obliga a re-cerrar". Nothing can force a
            # person's hand, so the day says so itself until it is closed again.
            warnings.append("This day was closed and reopened: it has to be closed again.")

        return warnings

    async def _was_reopened(self, day: date) -> bool:
        history = await self.repository.list_between(
            since=day, until=day, include_reopened=True
        )
        return any(closure.is_reopened for closure in history)

    # -- filing it ---------------------------------------------------------

    async def close(self, data: DailyCloseCreate, *, actor: User) -> DailyClosureRead:
        """Write the day down and lock it (D9).

        Every figure is stored even though every figure is derivable. That is the
        point of an acta: it says what the numbers were the evening somebody
        counted the drawer, and stays true when a category is renamed or the way
        a total is computed changes.
        """
        day = data.close_date or business_date()
        if day > business_date():
            raise ConflictError("A day that has not happened yet cannot be closed.")
        if await self.repository.get_for_date(day) is not None:
            raise ConflictError(f"Day {day} is already closed.")

        totals, delivered = await self._figures(day)
        closure = DailyClosure(
            **self._fields(day, totals, delivered),
            notes=data.notes,
            closed_by_id=actor.id,
            closed_at=datetime.now(UTC),
        )
        self.repository.add(closure)
        await self.repository.commit()
        return DailyClosureRead.model_validate(closure)

    @staticmethod
    def _fields(day: date, totals: DayTotals, delivered: int) -> dict[str, Any]:
        """The stored figures of §5.4, named the same on the record and on the
        preview. `income_total` and `cash_on_hand` are absent on purpose: both
        are sums of columns already here, and the schema derives them."""
        return {
            "close_date": day,
            "orders_income": totals.orders_income,
            "supplies_income": totals.supplies_income,
            "expenses_total": totals.expenses_total,
            "net_total": totals.net_total,
            "cash_income": totals.cash_income,
            "transfer_income": totals.transfer_income,
            "cash_expenses": totals.cash_expenses,
            "transfer_expenses": totals.transfer_expenses,
            "orders_delivered": delivered,
        }

    async def history(self, *, since: date, until: date) -> list[DailyClosureRead]:
        if since > until:
            raise ConflictError("The range starts after it ends.")
        closures = await self.repository.list_between(
            since=since, until=until, include_reopened=True
        )
        return [DailyClosureRead.model_validate(closure) for closure in closures]

    async def reopen(
        self, closure_id: UUID, data: DailyCloseReopen, *, actor: User
    ) -> DailyClosureRead:
        """Undo a close, with a name and a reason on it (D9).

        A tombstone rather than a delete, exactly like voiding an expense: the
        row stays, so a day that was closed at one figure and reopened is a fact
        somebody can find. Lifting `deleted_at` lifts the lock — the partial
        unique index then lets the date be closed again.
        """
        closure = await self.repository.get(closure_id)
        if closure is None:
            raise NotFoundError("Daily closure not found.")

        closure.deleted_at = datetime.now(UTC)
        closure.reopened_by_id = actor.id
        closure.reopen_reason = data.reason
        await self.repository.commit()
        return DailyClosureRead.model_validate(closure)
