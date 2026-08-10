from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.mixins import SyncableMixin


class DailyClosure(SyncableMixin, Base):
    """The day's record of account — the bottom of the paper sheet (§5.4).

    Every figure here is derivable from payments, sales and expenses, and every
    figure is stored anyway (D9). The close is an *acta*: what the numbers said
    the evening somebody counted the drawer. A report recomputed next year, after
    a category was renamed or a ticket corrected, answers a different question.
    """

    __tablename__ = "daily_closures"
    __table_args__ = (
        # §5.4 says the date is unique, and reopening is a tombstone (D9), so a
        # plain constraint would let a day be closed once and never again. Partial
        # on the live rows: one open close per date, and the reopened ones stay as
        # the trail of what was undone.
        Index(
            "uq_daily_closures_close_date",
            "close_date",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: Business date (D14). Not indexed on its own: the partial unique above
    #: starts with this column, and the historic listing filters by it.
    close_date: Mapped[date] = mapped_column(Date)

    #: Σ `order_payments` collected on the date, whatever ticket they settle (D1).
    orders_income: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: Σ of the counter sales of supplies that still stand.
    supplies_income: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    expenses_total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: Income less expenses. The Q764 of the real sheet (§6.1).
    net_total: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    #: Arqueo — how the money came in, so the drawer can be counted against it.
    cash_income: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    transfer_income: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: …and how it went out. Not in the §5.4 table, but the glossary asks the
    #: arqueo to square the physical box, and what is in the box at closing time
    #: is the cash that came in minus the cash that was paid out of it — the gas
    #: on the real sheet was Q161 in notes handed over the counter.
    cash_expenses: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    transfer_expenses: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    #: Tickets handed back on the date.
    orders_delivered: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    closed_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    #: Reopening is a tombstone with a name on it (D9), like voiding an expense:
    #: `deleted_at` lifts the lock, these two say who lifted it and why. The row
    #: stays, so a day closed at Q764 and reopened is a fact and not a gap.
    reopened_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def income_total(self) -> Decimal:
        return self.orders_income + self.supplies_income

    @property
    def cash_on_hand(self) -> Decimal:
        """What the drawer should hold: cash taken in, less cash paid out."""
        return self.cash_income - self.cash_expenses

    @property
    def is_reopened(self) -> bool:
        return self.deleted_at is not None
