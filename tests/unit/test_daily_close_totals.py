"""Adding up the day (Plan 0005 §6.1).

The reference is the real "Registro Diario" sheet of 18/07/26: Q855 in tickets,
Q140 in supplies, Q231 in expenses, Q764 left, and Q50 of the income arriving by
transfer. If any of these ever changes, the arithmetic moved — the day did not.
"""

from decimal import Decimal

from src.modules.daily_close.totals import Split, add_up
from src.modules.orders.models import PaymentMethod

CASH = PaymentMethod.CASH
TRANSFER = PaymentMethod.TRANSFER


def q(amount: str) -> Decimal:
    return Decimal(amount)


class TestSplit:
    def test_an_empty_day_is_zero_and_not_nothing(self) -> None:
        split = Split.of([])
        assert split.cash == q("0.00")
        assert split.transfer == q("0.00")
        assert split.total == q("0.00")

    def test_it_keeps_the_two_ways_the_money_moved_apart(self) -> None:
        split = Split.of([(q("945.00"), CASH), (q("50.00"), TRANSFER)])
        assert split.cash == q("945.00")
        assert split.transfer == q("50.00")
        assert split.total == q("995.00")

    def test_amounts_of_the_same_method_add_up(self) -> None:
        split = Split.of([(q("50.00"), CASH), (q("90.00"), CASH)])
        assert split.cash == q("140.00")
        assert split.transfer == q("0.00")

    def test_it_rounds_once_at_the_end_and_to_cents(self) -> None:
        split = Split.of([(q("0.005"), CASH), (q("0.005"), CASH)])
        assert split.cash == q("0.01")


class TestTheDayAddedUp:
    def test_the_bottom_of_the_real_sheet(self) -> None:
        """Q995 in, Q231 out, Q764 left — the figures written on the paper."""
        totals = add_up(
            orders=Split.of([(q("805.00"), CASH), (q("50.00"), TRANSFER)]),
            supplies=Split.of([(q("50.00"), CASH), (q("90.00"), CASH)]),
            expenses=q("231.00"),
            paid_expenses=Split.of([(q("231.00"), CASH)]),
        )

        assert totals.orders_income == q("855.00")
        assert totals.supplies_income == q("140.00")
        assert totals.income_total == q("995.00")
        assert totals.expenses_total == q("231.00")
        assert totals.net_total == q("764.00")

    def test_the_arqueo_splits_the_income_the_way_the_sheet_marks_it(self) -> None:
        """The pink row is the transfer: Q945 in notes, Q50 through the bank."""
        totals = add_up(
            orders=Split.of([(q("805.00"), CASH), (q("50.00"), TRANSFER)]),
            supplies=Split.of([(q("140.00"), CASH)]),
            expenses=q("231.00"),
            paid_expenses=Split.of([(q("231.00"), CASH)]),
        )

        assert totals.cash_income == q("945.00")
        assert totals.transfer_income == q("50.00")
        assert totals.cash_income + totals.transfer_income == totals.income_total

    def test_the_drawer_is_what_came_in_less_what_was_paid_out_of_it(self) -> None:
        """Q945 taken in notes, Q231 handed back out: Q714 should be in the box."""
        totals = add_up(
            orders=Split.of([(q("805.00"), CASH), (q("50.00"), TRANSFER)]),
            supplies=Split.of([(q("140.00"), CASH)]),
            expenses=q("231.00"),
            paid_expenses=Split.of([(q("231.00"), CASH)]),
        )

        assert totals.cash_expenses == q("231.00")
        assert totals.cash_on_hand == q("714.00")

    def test_an_expense_paid_by_transfer_never_touches_the_box(self) -> None:
        totals = add_up(
            orders=Split.of([(q("100.00"), CASH)]),
            supplies=Split(),
            expenses=q("40.00"),
            paid_expenses=Split.of([(q("40.00"), TRANSFER)]),
        )

        assert totals.cash_expenses == q("0.00")
        assert totals.transfer_expenses == q("40.00")
        assert totals.cash_on_hand == q("100.00")

    def test_what_is_owed_counts_against_the_day_but_not_against_the_box(self) -> None:
        """The sheet's "pago atrasado": written down today, paid later.

        It is the day's expense — the net total says so — and yet the drawer
        never saw it, which is why the two figures are not the same number.
        """
        totals = add_up(
            orders=Split.of([(q("500.00"), CASH)]),
            supplies=Split(),
            expenses=q("200.00"),
            paid_expenses=Split.of([(q("120.00"), CASH)]),
        )

        assert totals.expenses_total == q("200.00")
        assert totals.net_total == q("300.00")
        assert totals.cash_expenses == q("120.00")
        assert totals.cash_on_hand == q("380.00")

    def test_a_day_that_spent_more_than_it_took_goes_negative(self) -> None:
        """A day of buying stock and selling little. The close says so rather
        than clamping at zero: a negative net is a fact somebody has to see."""
        totals = add_up(
            orders=Split.of([(q("100.00"), CASH)]),
            supplies=Split(),
            expenses=q("450.00"),
            paid_expenses=Split.of([(q("450.00"), CASH)]),
        )

        assert totals.net_total == q("-350.00")
        assert totals.cash_on_hand == q("-350.00")

    def test_a_day_with_nothing_in_it_still_closes_at_zero(self) -> None:
        totals = add_up(
            orders=Split(), supplies=Split(), expenses=Decimal("0.00"), paid_expenses=Split()
        )

        assert totals.income_total == q("0.00")
        assert totals.net_total == q("0.00")
        assert totals.cash_on_hand == q("0.00")
