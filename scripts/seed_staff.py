"""Seed the two work shifts and the overtime rate (Plan 0005 §8.3).

Idempotent: a shift is matched by its code and an open rate by its code, and
neither is rewritten. Both are things the business changes — the hours are still
a hypothesis (§10 question 1, taken from the 6:50 entries and 18:50 exits on the
paper sheet), and the rate is administered from the app afterwards. A re-run that
put Q20 back over a raise would be the seeder deciding somebody's pay.

Employees are *not* seeded: they are real people, and inventing them would put
names nobody recognizes on the attendance screen.

    python -m scripts.seed_staff
"""

import asyncio
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.modules.staff.models import OVERTIME_RATE_CODE, PayrollRate, WorkShift

#: Kept in the past so a working day recorded today finds the rate in force.
VALID_FROM = date(2026, 1, 1)

#: (code, name, starts_at, ends_at, sort_order)
SHIFTS: tuple[tuple[str, str, time, time, int], ...] = (
    ("T1", "Turno mañana", time(7, 0), time(12, 0), 1),
    ("T2", "Turno tarde", time(13, 0), time(18, 0), 2),
)

#: Q20 the hour, prorated by the minute — the "Extra Q10" for half an hour on the
#: sheet the plan was written from.
OVERTIME_HOURLY = Decimal("20.00")


async def seed_staff() -> None:
    async with AsyncSessionFactory() as session:
        created_shifts = 0
        for code, name, starts_at, ends_at, sort_order in SHIFTS:
            existing = await session.scalar(select(WorkShift).where(WorkShift.code == code))
            if existing is not None:
                continue
            session.add(
                WorkShift(
                    code=code,
                    name=name,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    sort_order=sort_order,
                )
            )
            created_shifts += 1

        open_rate = await session.scalar(
            select(PayrollRate).where(
                PayrollRate.code == OVERTIME_RATE_CODE, PayrollRate.valid_to.is_(None)
            )
        )
        created_rate = open_rate is None
        if created_rate:
            session.add(
                PayrollRate(
                    code=OVERTIME_RATE_CODE, amount=OVERTIME_HOURLY, valid_from=VALID_FROM
                )
            )

        await session.commit()
        print(
            f"Staff seeded: {created_shifts} of {len(SHIFTS)} shifts created, "
            f"overtime rate {'created' if created_rate else 'already in force'}."
        )


if __name__ == "__main__":
    asyncio.run(seed_staff())
