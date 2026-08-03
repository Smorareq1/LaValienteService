"""The laundry's calendar day (Plan 0001 D8).

An order belongs to a *business* date, not to a UTC one: the daily correlative
restarts with it and the future daily close is cut by it. A ticket taken at
19:00 in Cobán is 01:00 UTC of the next day, and filing it under tomorrow would
put it in the wrong close.

Audit timestamps stay UTC, as everywhere else in the codebase.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

#: Guatemala has been on UTC-6 all year round since 2006, so the business day is
#: a fixed offset rather than a `ZoneInfo` lookup: the IANA database is not
#: installed on every machine that runs this code, and a missing time zone must
#: not silently change what day an order belongs to.
GUATEMALA = timezone(timedelta(hours=-6), name="America/Guatemala")


def business_date(moment: datetime | None = None) -> date:
    """The laundry's date at `moment` (default: now)."""
    return (moment or datetime.now(UTC)).astimezone(GUATEMALA).date()
