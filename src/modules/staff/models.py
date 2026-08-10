from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.core.mixins import SyncableMixin
from src.modules.staff.overtime import minutes_between

#: The rate code the overtime of Plan 0005 §6.2 is paid at. A code and not a
#: column of its own so a second concept (a night bonus, a holiday rate) is a
#: seeder row rather than a migration.
OVERTIME_RATE_CODE = "overtime_hour"


class Employee(SyncableMixin, Base):
    """Someone who works here, with or without a login (D6).

    Not a `User`: the laundry has people who clock in and never touch the system,
    and administrators who use it every day and never clock in. Tying the two
    together would force an account for every hire and an attendance record for
    every account.
    """

    __tablename__ = "employees"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Optional link to an account. Unique, and nullable: PostgreSQL lets a unique
    #: index hold any number of nulls, which is exactly "at most one employee per
    #: user, and most employees have none".
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkShift(SyncableMixin, Base):
    """One of the fixed blocks the day is worked in (§3: two of five hours).

    Administrable rather than hardcoded (D7), because "two shifts of five hours"
    is how the business runs today, not a law of the domain.
    """

    __tablename__ = "work_shifts"
    __table_args__ = (
        # A shift that ended before it started would make `duration_minutes`
        # negative and every overtime suggestion built on it wrong. Crossing
        # midnight is not representable here either — see `AttendanceRecord`.
        CheckConstraint("ends_at > starts_at", name="ck_work_shifts_ends_after_start"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    #: Business-local wall clock, like everything else a person reads off a
    #: schedule. Nothing here is converted to UTC: 07:00 is 07:00 in Cobán.
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def duration_minutes(self) -> int:
        """How long the shift is scheduled for — what overtime is measured past."""
        return minutes_between(self.starts_at, self.ends_at)

    @property
    def window(self) -> tuple[time, time]:
        return self.starts_at, self.ends_at


class PayrollRate(SyncableMixin, Base):
    """What an hour of something is paid at, with the window it applied in (D7).

    Same shape as `service_prices` and for the same reason: raising the overtime
    rate must not rewrite what was paid last month.
    """

    __tablename__ = "payroll_rates"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payroll_rates_amount_positive"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_payroll_rates_window_ordered"
        ),
        Index("ix_payroll_rates_code_valid_from", "code", "valid_from"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    valid_from: Mapped[date] = mapped_column(Date)
    #: `None` means this is the rate in force today.
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def covers(self, on_date: date) -> bool:
        return self.valid_from <= on_date and (self.valid_to is None or on_date <= self.valid_to)


class AttendanceRecord(SyncableMixin, Base):
    """A worked day: what today is written as "Entró 6:50 / Salió 13:00" (§5.1).

    Times and not timestamps, which is the plan's choice and carries a limit
    worth naming: a shift running past midnight cannot be told apart from a typo,
    so both are refused by `ck_attendance_records_out_after_in`. The two day
    shifts of §3 never cross it.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        CheckConstraint(
            "clock_out IS NULL OR clock_out > clock_in",
            name="ck_attendance_records_out_after_in",
        ),
        CheckConstraint(
            "overtime_minutes >= 0", name="ck_attendance_records_overtime_not_negative"
        ),
        Index("ix_attendance_records_employee_work_date", "employee_id", "work_date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # `ix_attendance_records_employee_work_date` already leads with this column;
    # a second index on it alone can never win and costs a write on every clock-in.
    employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.id"))
    work_date: Mapped[date] = mapped_column(Date, index=True)
    #: `None` is a stretch worked outside any shift, which the paper sheet also
    #: records — it is not missing data.
    shift_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("work_shifts.id"), nullable=True, index=True
    )
    clock_in: Mapped[time] = mapped_column(Time)
    #: `None` while the person has not clocked out yet.
    clock_out: Mapped[time | None] = mapped_column(Time, nullable=True)
    #: Minutes **confirmed** for payment, never the raw excess: the system works
    #: out a suggestion and a person decides (D8). Zero until someone does.
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_open(self) -> bool:
        """Whether the person is still working: clocked in, not yet out."""
        return self.clock_out is None

    @property
    def worked_minutes(self) -> int | None:
        """Minutes on the clock. `None` while the day is still open."""
        if self.clock_out is None:
            return None
        return minutes_between(self.clock_in, self.clock_out)
