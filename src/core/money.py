"""How every amount in the system is rounded (Plan 0005 D14).

One rule in one place. Tickets, supply sales, expenses and the daily close all
add up to figures a person will compare against cash in a drawer, and two modules
rounding differently is how a close ends off by a cent that nobody can find.

Written for the ticket engine first (Plan 0001 §6) and moved here when the second
module needed it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Round to cents, half away from zero — how a person rounds on paper.

    Applied per line and then summed, rather than summing exact values and
    rounding at the end, so the printed lines add up to the printed total. A
    customer checking the ticket by hand must not find it off by a cent.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)
