from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.models import User
from src.modules.staff.models import AttendanceRecord, Employee, PayrollRate, WorkShift


class StaffRepository:
    """Persistence for people, shifts, rates and worked days. No arithmetic."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- employees ---------------------------------------------------------

    async def list_employees(self, *, include_inactive: bool = False) -> list[Employee]:
        statement = select(Employee).where(Employee.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(Employee.is_active.is_(True))
        statement = statement.order_by(Employee.full_name)
        return list((await self.session.scalars(statement)).all())

    async def get_employee(self, employee_id: UUID) -> Employee | None:
        statement = select(Employee).where(
            Employee.id == employee_id, Employee.deleted_at.is_(None)
        )
        employee: Employee | None = await self.session.scalar(statement)
        return employee

    async def get_employee_by_user(self, user_id: UUID) -> Employee | None:
        statement = select(Employee).where(
            Employee.user_id == user_id, Employee.deleted_at.is_(None)
        )
        employee: Employee | None = await self.session.scalar(statement)
        return employee

    async def user_exists(self, user_id: UUID) -> bool:
        """Whether the account an employee is being linked to is real.

        Only the id: the check is "does this row exist", and loading a user with
        its roles and permissions to answer it would be a much heavier query
        than the question deserves.
        """
        return await self.session.scalar(select(User.id).where(User.id == user_id)) is not None

    # -- shifts ------------------------------------------------------------

    async def list_shifts(self, *, include_inactive: bool = False) -> list[WorkShift]:
        statement = select(WorkShift).where(WorkShift.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(WorkShift.is_active.is_(True))
        statement = statement.order_by(WorkShift.sort_order, WorkShift.starts_at)
        return list((await self.session.scalars(statement)).all())

    async def get_shift(self, shift_id: UUID) -> WorkShift | None:
        statement = select(WorkShift).where(
            WorkShift.id == shift_id, WorkShift.deleted_at.is_(None)
        )
        shift: WorkShift | None = await self.session.scalar(statement)
        return shift

    async def get_shift_by_code(self, code: str) -> WorkShift | None:
        statement = select(WorkShift).where(WorkShift.code == code, WorkShift.deleted_at.is_(None))
        shift: WorkShift | None = await self.session.scalar(statement)
        return shift

    # -- rates -------------------------------------------------------------

    async def list_rates(self, code: str | None = None) -> list[PayrollRate]:
        """Every window, newest first — the history the settings screen shows."""
        statement = select(PayrollRate).where(PayrollRate.deleted_at.is_(None))
        if code is not None:
            statement = statement.where(PayrollRate.code == code)
        statement = statement.order_by(PayrollRate.code, PayrollRate.valid_from.desc())
        return list((await self.session.scalars(statement)).all())

    async def get_open_rate(self, code: str) -> PayrollRate | None:
        """The window with no end date: the rate a new one has to close."""
        statement = select(PayrollRate).where(
            PayrollRate.code == code,
            PayrollRate.valid_to.is_(None),
            PayrollRate.deleted_at.is_(None),
        )
        rate: PayrollRate | None = await self.session.scalar(statement)
        return rate

    async def get_rate_on(self, code: str, on_date: date) -> PayrollRate | None:
        """The rate in force on a date. Windows do not overlap; newest still wins.

        `register_rate` closes the previous window before opening the next, so
        there is only ever one candidate — but a bad import must price at the
        most recent intent rather than at whatever the planner returned first.
        """
        statement = (
            select(PayrollRate)
            .where(
                PayrollRate.code == code,
                PayrollRate.deleted_at.is_(None),
                PayrollRate.valid_from <= on_date,
                (PayrollRate.valid_to.is_(None)) | (PayrollRate.valid_to >= on_date),
            )
            .order_by(PayrollRate.valid_from.desc())
            .limit(1)
        )
        rate: PayrollRate | None = await self.session.scalar(statement)
        return rate

    # -- attendance --------------------------------------------------------

    async def list_attendance(
        self, *, work_date: date | None = None, employee_id: UUID | None = None
    ) -> list[AttendanceRecord]:
        statement = select(AttendanceRecord).where(AttendanceRecord.deleted_at.is_(None))
        if work_date is not None:
            statement = statement.where(AttendanceRecord.work_date == work_date)
        if employee_id is not None:
            statement = statement.where(AttendanceRecord.employee_id == employee_id)
        statement = statement.order_by(
            AttendanceRecord.work_date.desc(), AttendanceRecord.clock_in
        )
        return list((await self.session.scalars(statement)).all())

    async def get_attendance(self, record_id: UUID) -> AttendanceRecord | None:
        statement = select(AttendanceRecord).where(
            AttendanceRecord.id == record_id, AttendanceRecord.deleted_at.is_(None)
        )
        record: AttendanceRecord | None = await self.session.scalar(statement)
        return record

    async def get_open_attendance(
        self, employee_id: UUID, work_date: date
    ) -> AttendanceRecord | None:
        """A day already started and not yet finished, if there is one."""
        statement = select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.work_date == work_date,
            AttendanceRecord.clock_out.is_(None),
            AttendanceRecord.deleted_at.is_(None),
        )
        record: AttendanceRecord | None = await self.session.scalar(statement)
        return record

    # -- writes ------------------------------------------------------------

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
