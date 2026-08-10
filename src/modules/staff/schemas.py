from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"


class EmployeeCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    #: Optional account for this person (D6): most of the staff have none.
    user_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)


class EmployeeUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    user_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone: str | None
    user_id: UUID | None
    notes: str | None
    is_active: bool
    version: int


class WorkShiftCreate(BaseModel):
    code: str = Field(pattern=CODE_PATTERN, max_length=20)
    name: str = Field(min_length=1, max_length=80)
    starts_at: time
    ends_at: time
    sort_order: int = 0

    @model_validator(mode="after")
    def _check_window(self) -> WorkShiftCreate:
        if self.ends_at <= self.starts_at:
            raise ValueError("A shift has to end after it starts, on the same day.")
        return self


class WorkShiftUpdate(BaseModel):
    """Everything but the code, which is what a seeder and a schedule name it by."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    starts_at: time | None = None
    ends_at: time | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class WorkShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    starts_at: time
    ends_at: time
    is_active: bool
    sort_order: int
    version: int
    #: How long the shift is scheduled for. Derived, but sent: it is what the
    #: overtime suggestion is measured against, and the app shows it.
    duration_minutes: int


class PayrollRateCreate(BaseModel):
    """A new validity window. The one in force is closed the day before."""

    code: str = Field(pattern=CODE_PATTERN, max_length=50)
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    valid_from: date


class PayrollRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    amount: Decimal
    valid_from: date
    valid_to: date | None
    version: int


class AttendanceCreate(BaseModel):
    """Clocking in: who, which day, at what time, under which shift."""

    #: Minted by the device when the day is clocked in offline (Plan 0004), so a
    #: push that never got its answer back can be re-sent without opening a
    #: second working day for the same person.
    id: UUID | None = None
    employee_id: UUID
    work_date: date
    shift_id: UUID | None = None
    clock_in: time
    clock_out: time | None = None
    #: Accepted on creation because a jornada is often written down after the
    #: fact, off the paper sheet, when both times are already known.
    overtime_minutes: int = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_window(self) -> AttendanceCreate:
        if self.clock_out is not None and self.clock_out <= self.clock_in:
            raise ValueError("The end of a working day has to come after its start.")
        return self


class AttendanceUpdate(BaseModel):
    """Clocking out, confirming the extra minutes, or correcting either."""

    shift_id: UUID | None = None
    clock_in: time | None = None
    clock_out: time | None = None
    #: The minutes a person decided to pay (D8). The suggestion never writes
    #: itself here: that is the whole difference between the two fields.
    overtime_minutes: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class AttendanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    employee_id: UUID
    employee_name: str
    work_date: date
    shift_id: UUID | None
    clock_in: time
    clock_out: time | None
    overtime_minutes: int
    notes: str | None
    version: int
    #: Minutes actually on the clock; `None` while the person is still working.
    worked_minutes: int | None
    #: What the system would pay for, if someone confirms it (§6.2 step 2).
    suggested_overtime_minutes: int
    #: The confirmed minutes priced at the rate in force on `work_date`. `None`
    #: when no rate covers that date — which is a gap to fix, not a free hour.
    overtime_amount: Decimal | None
