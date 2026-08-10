"""The date lock of D9, as a contract the other modules can hold.

Closing a day freezes it: tickets, payments, expenses and supply sales of that
date stop moving. The check has to live in the services and not at the endpoints,
because an operation arriving from a device (Plan 0004 §6.4) must be refused by
the same rule as an HTTP request — and it reaches the service directly.

Which leaves a direction problem: `daily_close` reads `orders`, `inventory` and
`expenses` to add the day up, so those three cannot import it back. They import
*this* module instead, which knows nothing about the rest of the close — the same
inversion PR 8 used to book a lot's expense.

**What the lock is on:** the date of the event being written, not the age of the
paper it is written on. Clothes taken on Monday are still handed back on
Wednesday after Monday is closed — the delivery is Wednesday's event. What is
refused is anything that would change what a closed day already recorded.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from src.core.exceptions import ConflictError


class ClosedDays(Protocol):
    """Answers the only question the other services need to ask."""

    async def is_closed(self, day: date) -> bool: ...


async def ensure_open(days: ClosedDays | None, day: date) -> None:
    """Refuse to touch a day that has already been accounted for.

    `None` means no lock is wired — the case in unit tests, where there is no
    close to consult. Every path that serves a request passes one.
    """
    if days is None:
        return
    if await days.is_closed(day):
        raise ConflictError(
            f"Day {day} is already closed; ask an administrator to reopen it."
        )
