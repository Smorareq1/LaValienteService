"""What an extra hour is worth (Plan 0005 §6.2).

Pure arithmetic over times and a rate: no session, no HTTP. If one of these
breaks, somebody is paid the wrong amount for having stayed late.
"""

from datetime import time
from decimal import Decimal

from src.modules.staff.overtime import (
    minutes_between,
    overtime_amount,
    suggested_overtime_minutes,
)

MORNING = (time(7, 0), time(12, 0))
Q20 = Decimal("20.00")


class TestMinutesBetween:
    def test_counts_a_shift(self) -> None:
        assert minutes_between(time(7, 0), time(12, 0)) == 300

    def test_counts_the_odd_minutes_of_a_real_entry(self) -> None:
        """"Entró 6:50 / Salió 13:00" — the row the plan was written from."""
        assert minutes_between(time(6, 50), time(13, 0)) == 370


class TestSuggestedOvertime:
    def test_suggests_the_excess_over_the_shift(self) -> None:
        """6:50 to 13:00 is 6h10 against a 5h shift: 70 minutes over."""
        assert suggested_overtime_minutes(time(6, 50), time(13, 0), MORNING) == 70

    def test_half_an_hour_over(self) -> None:
        assert suggested_overtime_minutes(time(7, 0), time(12, 30), MORNING) == 30

    def test_leaving_early_is_not_negative_overtime(self) -> None:
        """Owing time is a conversation, not a number to pay backwards."""
        assert suggested_overtime_minutes(time(7, 0), time(11, 0), MORNING) == 0

    def test_an_open_day_suggests_nothing(self) -> None:
        assert suggested_overtime_minutes(time(7, 0), None, MORNING) == 0

    def test_without_a_shift_there_is_nothing_to_exceed(self) -> None:
        """A stretch worked outside any shift: a person says what is owed."""
        assert suggested_overtime_minutes(time(19, 0), time(21, 0), None) == 0


class TestOvertimeAmount:
    def test_half_an_hour_at_q20_is_q10(self) -> None:
        """The "Claudia Extra Q10" of the sheet, which is what fixes the policy."""
        assert overtime_amount(30, Q20) == Decimal("10.00")

    def test_a_full_hour(self) -> None:
        assert overtime_amount(60, Q20) == Decimal("20.00")

    def test_prorates_by_the_minute_and_rounds_to_cents(self) -> None:
        """70 minutes at Q20 is Q23.333…, and money has two decimals."""
        assert overtime_amount(70, Q20) == Decimal("23.33")

    def test_no_minutes_no_money(self) -> None:
        assert overtime_amount(0, Q20) == Decimal("0.00")

    def test_a_negative_count_never_becomes_a_charge(self) -> None:
        assert overtime_amount(-30, Q20) == Decimal("0.00")

    def test_follows_the_rate_it_is_given(self) -> None:
        """Rates are versioned (D7): the caller picks the window, not this."""
        assert overtime_amount(60, Decimal("25.00")) == Decimal("25.00")
