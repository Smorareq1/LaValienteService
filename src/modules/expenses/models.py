from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.mixins import SyncableMixin
from src.modules.orders.models import PaymentMethod

#: The category a lot purchase is booked under (§6.3). Named here and not in the
#: seeder because the code looks it up by name: if the two ever disagreed,
#: registering a lot with its expense would fail with nothing to point at.
SUPPLY_PURCHASE_CATEGORY = "Compra de insumos"

#: Where an overtime payment goes (§6.2 step 4). Same reasoning.
OVERTIME_CATEGORY = "Horas extra"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class ExpenseStatus(enum.StrEnum):
    """Whether the money has actually left (§5.3)."""

    PAID = "paid"
    #: The sheet's "pago atrasado" and "de realizar": written down on the day it
    #: was owed, settled later. It is still the day's expense — the paper counts
    #: it — but the drawer has not seen it yet.
    PENDING = "pending"


class ExpenseCategory(SyncableMixin, Base):
    """Administrable, like garment types (D7 of Plan 0001).

    Not an enum: "Secadora Q20" is still an open question (§10 question 3) and
    the answer must not be a migration.
    """

    __tablename__ = "expense_categories"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Expense(SyncableMixin, Base):
    """Money going out — the right-hand side of the sheet (§1).

    The three optional links are what turn a list of amounts into an account of
    the day: an overtime payment names the person and the working day it came
    from, and a purchase names the lot it put on the shelf.
    """

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
        # A jornada is paid once. Two expenses against one working day is not a
        # correction, it is somebody paid twice — and the amount is prefilled
        # from the record, so the second one looks just as right as the first.
        # Partial, so a voided payment frees the day to be paid properly.
        Index(
            "uq_expenses_attendance_record",
            "attendance_record_id",
            unique=True,
            postgresql_where=text("attendance_record_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: Business date (D14): what the daily close cuts by.
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("expense_categories.id"), index=True)
    #: The sheet's two columns joined into the sentence they always meant:
    #: "Gas — 2 sacos", "Claudia — hora extra".
    concept: Mapped[str] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method", values_callable=_enum_values),
        default=PaymentMethod.CASH,
    )
    status: Mapped[ExpenseStatus] = mapped_column(
        Enum(ExpenseStatus, name="expense_status", values_callable=_enum_values),
        default=ExpenseStatus.PAID,
        index=True,
    )
    employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    #: The working day this payment settles (D8). Its presence is what stops the
    #: same overtime being paid twice.
    attendance_record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attendance_records.id"), nullable=True
    )
    product_lot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("product_lots.id"), nullable=True, index=True
    )
    #: "Pendiente de entrega", "pieza pendiente", "cobro"… the sheet's margin.
    observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: Voiding is a tombstone with a name on it (§7). `deleted_at` says it is
    #: gone; these two say who decided that and why — the same trace a voided
    #: ticket carries.
    voided_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_paid(self) -> bool:
        return self.status is ExpenseStatus.PAID
