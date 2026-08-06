"""Adding up the day (§6.1). No database, no session — just arithmetic.

Pure like `orders/pricing.py` and `staff/overtime.py`, and for the same reason:
these are the figures somebody compares against the cash in a drawer, so they
have to be testable against the real sheet of 18/07/26 without standing up a
database to ask.

The three modules that hold money speak to the close through `Split`: how much
came in — or went out — in notes, and how much through the bank.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from src.core.money import money
from src.modules.orders.models import PaymentMethod

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Split:
    """An amount of money broken down the two ways the laundry takes it.

    The pink row of the paper sheet is a transfer; everything unmarked is cash.
    Keeping the two apart is the whole point of the arqueo: the drawer can only
    be counted against the cash half.
    """

    cash: Decimal = ZERO
    transfer: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.cash + self.transfer

    @classmethod
    def of(cls, amounts: Iterable[tuple[Decimal, PaymentMethod]]) -> Split:
        """Add up `(amount, method)` pairs — how each module reports its day."""
        cash = ZERO
        transfer = ZERO
        for amount, method in amounts:
            if method is PaymentMethod.TRANSFER:
                transfer += amount
            else:
                cash += amount
        return cls(cash=money(cash), transfer=money(transfer))


@dataclass(frozen=True)
class DayTotals:
    """The bottom of the sheet: Q995 in, Q231 out, Q764 left (§6.1)."""

    orders_income: Decimal
    supplies_income: Decimal
    income_total: Decimal
    expenses_total: Decimal
    net_total: Decimal
    cash_income: Decimal
    transfer_income: Decimal
    cash_expenses: Decimal
    transfer_expenses: Decimal
    cash_on_hand: Decimal


def add_up(*, orders: Split, supplies: Split, expenses: Decimal, paid_expenses: Split) -> DayTotals:
    """The day's account, from the three sources that hold money.

    `expenses` is everything the day owes and `paid_expenses` only what has
    actually left, which is not the same number: the sheet's "pago atrasado"
    counts against the day — the paper writes it down — while the drawer never
    saw it. The net total uses the first, the arqueo the second, and a day with
    nothing pending makes them agree.
    """
    income_total = money(orders.total + supplies.total)
    return DayTotals(
        orders_income=orders.total,
        supplies_income=supplies.total,
        income_total=income_total,
        expenses_total=money(expenses),
        net_total=money(income_total - expenses),
        cash_income=orders.cash + supplies.cash,
        transfer_income=orders.transfer + supplies.transfer,
        cash_expenses=paid_expenses.cash,
        transfer_expenses=paid_expenses.transfer,
        cash_on_hand=money(orders.cash + supplies.cash - paid_expenses.cash),
    )
