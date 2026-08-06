from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field


class DayFigures(BaseModel):
    """The numbers of §6.1, whether still moving or already frozen.

    Shared by the preview and the record so the close screen reads one shape all
    day: what it shows at four in the afternoon and what it shows after the
    drawer is counted differ in whether they can still change, not in what they
    mean.
    """

    close_date: date
    orders_income: Decimal
    supplies_income: Decimal
    expenses_total: Decimal
    net_total: Decimal
    cash_income: Decimal
    transfer_income: Decimal
    #: Only what has actually left: an expense still `pending` counts against the
    #: day but never came out of the drawer.
    cash_expenses: Decimal
    transfer_expenses: Decimal
    orders_delivered: int

    # The two sums below are derived here rather than stored and sent, so that a
    # column and a total can never come back disagreeing with each other.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def income_total(self) -> Decimal:
        return self.orders_income + self.supplies_income

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash_on_hand(self) -> Decimal:
        """What the box should hold when counted: cash in, less cash paid out."""
        return self.cash_income - self.cash_expenses


class DailyClosePreview(DayFigures):
    """The sheet on screen while the day is still open (§6.1)."""

    #: Things worth reading before closing, never a reason to refuse: tickets not
    #: handed back, money still owed, expenses left pending.
    warnings: list[str] = Field(default_factory=list)
    is_closed: bool = False
    #: Present once the day is closed — what the snapshot below was filed under.
    closure_id: UUID | None = None


class DailyCloseCreate(BaseModel):
    #: Defaults to today's business date, which is what the counter means.
    close_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class DailyCloseReopen(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class DailyClosureRead(DayFigures):
    """The acta as filed (D9)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    notes: str | None
    closed_by_id: UUID
    closed_at: datetime
    #: Set when the day was reopened: the row stays as the trail of what was
    #: undone, and the date is free to be closed again.
    reopened_by_id: UUID | None = None
    reopen_reason: str | None = None
    version: int
