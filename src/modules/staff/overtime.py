"""What an extra hour is worth (Plan 0005 §6.2).

Pure logic: no session, no FastAPI, no I/O — the same reason the ticket engine
lives in `orders/pricing.py`. What a person gets paid must be readable and
testable without a database around it.

The chain the plan describes has four links and this module owns two of them:
the system *suggests* the minutes (:func:`suggested_overtime_minutes`) and prices
the minutes a person *confirmed* (:func:`overtime_amount`). Confirming is not
here, and neither is paying: the payment is an expense of the day (D8), created
in the `expenses` module.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from src.core.money import money

MINUTES_PER_HOUR = 60


def minutes_between(start: time, end: time) -> int:
    """Wall-clock minutes from `start` to `end`, both on the same day.

    Negative if `end` comes first, which the callers never let happen: both the
    shift and the worked day refuse it at the database (`ends_at > starts_at`,
    `clock_out > clock_in`). A day crossing midnight is not representable with
    `Time` columns, and the plan's two five-hour day shifts never do.
    """
    return (end.hour * MINUTES_PER_HOUR + end.minute) - (
        start.hour * MINUTES_PER_HOUR + start.minute
    )


def suggested_overtime_minutes(
    clock_in: time, clock_out: time | None, shift: tuple[time, time] | None
) -> int:
    """Minutes worked beyond the shift — a suggestion, never a decision (D8).

    Zero while the person has not clocked out, and zero for a stretch worked
    outside any shift: there is no scheduled length to exceed, so the system has
    nothing to suggest and someone has to say what is owed. Zero here means "no
    suggestion", which is why the caller keeps it separate from the confirmed
    `overtime_minutes` rather than writing it there.
    """
    if clock_out is None or shift is None:
        return 0
    worked = minutes_between(clock_in, clock_out)
    scheduled = minutes_between(*shift)
    return max(worked - scheduled, 0)


def overtime_amount(minutes: int, hourly_rate: Decimal) -> Decimal:
    """What `minutes` of overtime cost at `hourly_rate`, prorated by the minute.

    Half an hour at Q20 is Q10 — the "Claudia Extra Q10" of the sheet the plan
    was written from. Prorating and not rounding up to the hour is the business's
    own policy, read off that row.
    """
    if minutes <= 0:
        return money(Decimal(0))
    return money(Decimal(minutes) * hourly_rate / Decimal(MINUTES_PER_HOUR))
