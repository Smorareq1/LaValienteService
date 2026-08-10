from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.modules.staff.models import (
    OVERTIME_RATE_CODE,
    AttendanceRecord,
    Employee,
    PayrollRate,
    WorkShift,
)
from src.modules.staff.overtime import overtime_amount, suggested_overtime_minutes
from src.modules.staff.repository import StaffRepository
from src.modules.staff.schemas import (
    AttendanceCreate,
    AttendanceRead,
    AttendanceUpdate,
    EmployeeCreate,
    EmployeeUpdate,
    PayrollRateCreate,
    WorkShiftCreate,
    WorkShiftUpdate,
)


class StaffService:
    """People, the hours they work, and what an extra one is paid at.

    The arithmetic of overtime is not here — it is in `overtime.py`, so what a
    person gets paid can be read without the persistence around it. This class
    decides *when* to ask for it and what is allowed to be written down.
    """

    def __init__(self, repository: StaffRepository) -> None:
        self.repository = repository

    # -- employees ---------------------------------------------------------

    async def list_employees(self, *, include_inactive: bool = False) -> list[Employee]:
        return await self.repository.list_employees(include_inactive=include_inactive)

    async def get_employee(self, employee_id: UUID) -> Employee:
        employee = await self.repository.get_employee(employee_id)
        if employee is None:
            raise NotFoundError("Employee not found.")
        return employee

    async def create_employee(self, data: EmployeeCreate) -> Employee:
        await self._check_user_link(data.user_id, employee_id=None)
        employee = Employee(
            full_name=data.full_name,
            phone=data.phone,
            user_id=data.user_id,
            notes=data.notes,
        )
        self.repository.add(employee)
        await self.repository.commit()
        return employee

    async def update_employee(self, employee_id: UUID, data: EmployeeUpdate) -> Employee:
        employee = await self.get_employee(employee_id)
        changes = data.model_dump(exclude_unset=True)

        if "user_id" in changes:
            await self._check_user_link(changes["user_id"], employee_id=employee_id)

        for field, value in changes.items():
            setattr(employee, field, value)

        # Deactivating is not a tombstone, unlike archiving a customer: worked
        # days and overtime payments point at this row, and a device that dropped
        # it would show last month's attendance with no name on it. Someone who
        # left stops appearing in the day's list and stays readable in history.
        await self.repository.commit()
        return employee

    async def _check_user_link(self, user_id: UUID | None, *, employee_id: UUID | None) -> None:
        """An account belongs to at most one employee, and has to exist (D6)."""
        if user_id is None:
            return
        if not await self.repository.user_exists(user_id):
            raise NotFoundError("That user account does not exist.")
        existing = await self.repository.get_employee_by_user(user_id)
        if existing is not None and existing.id != employee_id:
            raise ConflictError(
                f"That user account is already linked to {existing.full_name}."
            )

    # -- shifts ------------------------------------------------------------

    async def list_shifts(self, *, include_inactive: bool = False) -> list[WorkShift]:
        return await self.repository.list_shifts(include_inactive=include_inactive)

    async def create_shift(self, data: WorkShiftCreate) -> WorkShift:
        if await self.repository.get_shift_by_code(data.code) is not None:
            raise ConflictError(f"A shift with code '{data.code}' already exists.")
        shift = WorkShift(
            code=data.code,
            name=data.name,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            sort_order=data.sort_order,
        )
        self.repository.add(shift)
        await self.repository.commit()
        return shift

    async def update_shift(self, shift_id: UUID, data: WorkShiftUpdate) -> WorkShift:
        shift = await self.repository.get_shift(shift_id)
        if shift is None:
            raise NotFoundError("Work shift not found.")

        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(shift, field, value)

        # Checked after applying, because either end may be the one moving:
        # pushing the start past the end and pulling the end before the start are
        # the same mistake, and either one makes every overtime suggestion built
        # on this shift negative.
        if shift.ends_at <= shift.starts_at:
            raise ConflictError("A shift has to end after it starts, on the same day.")

        await self.repository.commit()
        return shift

    # -- rates -------------------------------------------------------------

    async def list_rates(self, code: str | None = None) -> list[PayrollRate]:
        return await self.repository.list_rates(code)

    async def register_rate(self, data: PayrollRateCreate) -> PayrollRate:
        """Open a new rate window, closing the one in force (D7).

        Both writes in one transaction, for the same reason prices do it: two
        open windows would make what an hour costs depend on query order — and
        here that is somebody's pay.
        """
        current = await self.repository.get_open_rate(data.code)
        if current is not None:
            if data.valid_from <= current.valid_from:
                raise ConflictError(
                    "The new rate must start after the one in force "
                    f"({current.valid_from.isoformat()})."
                )
            current.valid_to = data.valid_from - timedelta(days=1)

        rate = PayrollRate(code=data.code, amount=data.amount, valid_from=data.valid_from)
        self.repository.add(rate)
        await self.repository.commit()
        return rate

    async def overtime_rate_on(self, on_date: date) -> PayrollRate | None:
        """What an extra hour was paid at on a date. `None` if nothing covers it."""
        return await self.repository.get_rate_on(OVERTIME_RATE_CODE, on_date)

    # -- attendance --------------------------------------------------------

    async def list_attendance(
        self, *, work_date: date | None = None, employee_id: UUID | None = None
    ) -> list[AttendanceRead]:
        records = await self.repository.list_attendance(
            work_date=work_date, employee_id=employee_id
        )
        return await self._read_all(records)

    async def get_attendance(self, record_id: UUID) -> AttendanceRecord:
        record = await self.repository.get_attendance(record_id)
        if record is None:
            raise NotFoundError("Attendance record not found.")
        return record

    async def clock_in(self, data: AttendanceCreate) -> AttendanceRead:
        """Start a working day (§6.2 step 1)."""
        if data.id is not None and await self.repository.get_attendance(data.id) is not None:
            # A device re-sending an operation whose answer it never saw. The
            # working day exists; saying so is not a problem anyone has to solve.
            raise ConflictError("That working day is already registered.")

        employee = await self.get_employee(data.employee_id)
        if not employee.is_active:
            raise ConflictError(f"{employee.full_name} is no longer on the staff.")
        await self._require_shift(data.shift_id)

        open_record = await self.repository.get_open_attendance(data.employee_id, data.work_date)
        if open_record is not None:
            # Two open days for one person is always the same slip: tapping
            # "entrada" again instead of "salida". Letting it through would leave
            # a day nobody ever closes and an overtime suggestion of zero.
            raise ConflictError(
                f"{employee.full_name} already has an open working day on "
                f"{data.work_date.isoformat()}."
            )

        record = AttendanceRecord(
            id=data.id or uuid4(),
            employee_id=data.employee_id,
            work_date=data.work_date,
            shift_id=data.shift_id,
            clock_in=data.clock_in,
            clock_out=data.clock_out,
            overtime_minutes=data.overtime_minutes,
            notes=data.notes,
        )
        self.repository.add(record)
        await self.repository.commit()
        return await self._read_one(record, employee)

    async def update_attendance(
        self, record_id: UUID, data: AttendanceUpdate, *, base_version: int | None = None
    ) -> AttendanceRead:
        """Clock out, confirm the extra minutes, or correct what was written.

        Confirming is a write like any other: the suggestion of §6.2 step 2 is
        computed on every read and never stored, so a person deciding to pay
        nothing is a decision the record keeps, not one the next read undoes.
        """
        record = await self.get_attendance(record_id)
        if base_version is not None and record.version != base_version:
            # Two devices clocking the same person out is the case this exists
            # for: whichever arrives second would otherwise overwrite an hour it
            # never saw (D6).
            raise StaleVersionError(
                f"That working day changed since version {base_version} "
                f"(current version is {record.version})."
            )
        changes = data.model_dump(exclude_unset=True)

        if "shift_id" in changes:
            await self._require_shift(changes["shift_id"])

        for field, value in changes.items():
            setattr(record, field, value)

        if record.clock_out is not None and record.clock_out <= record.clock_in:
            raise ConflictError("The end of a working day has to come after its start.")

        await self.repository.commit()
        return await self._read_one(record, await self.get_employee(record.employee_id))

    async def _require_shift(self, shift_id: UUID | None) -> WorkShift | None:
        if shift_id is None:
            return None
        shift = await self.repository.get_shift(shift_id)
        if shift is None:
            raise NotFoundError("Work shift not found.")
        return shift

    # -- reads -------------------------------------------------------------

    async def _read_one(self, record: AttendanceRecord, employee: Employee) -> AttendanceRead:
        shift = await self._require_shift(record.shift_id)
        rate = await self.overtime_rate_on(record.work_date)
        return self._to_read(record, employee.full_name, shift, rate)

    async def _read_all(self, records: list[AttendanceRecord]) -> list[AttendanceRead]:
        """Build the reads with a handful of queries, not a handful per row.

        A month of attendance is a few hundred rows over one staff, two shifts
        and one or two rates; looking any of them up per record would turn a
        screen into a fan-out for values that barely change. Inactive rows are
        included on purpose: someone who left still has last month's days, and
        the name on them has to keep showing.
        """
        names = {
            person.id: person.full_name
            for person in await self.repository.list_employees(include_inactive=True)
        }
        shifts = {
            shift.id: shift for shift in await self.repository.list_shifts(include_inactive=True)
        }
        rates: dict[date, PayrollRate | None] = {}
        reads: list[AttendanceRead] = []
        for record in records:
            if record.work_date not in rates:
                rates[record.work_date] = await self.overtime_rate_on(record.work_date)
            reads.append(
                self._to_read(
                    record,
                    names.get(record.employee_id, "—"),
                    shifts.get(record.shift_id) if record.shift_id else None,
                    rates[record.work_date],
                )
            )
        return reads

    @staticmethod
    def _to_read(
        record: AttendanceRecord,
        employee_name: str,
        shift: WorkShift | None,
        rate: PayrollRate | None,
    ) -> AttendanceRead:
        return AttendanceRead(
            id=record.id,
            employee_id=record.employee_id,
            employee_name=employee_name,
            work_date=record.work_date,
            shift_id=record.shift_id,
            clock_in=record.clock_in,
            clock_out=record.clock_out,
            overtime_minutes=record.overtime_minutes,
            notes=record.notes,
            version=record.version,
            worked_minutes=record.worked_minutes,
            suggested_overtime_minutes=suggested_overtime_minutes(
                record.clock_in, record.clock_out, shift.window if shift is not None else None
            ),
            # Priced off the confirmed minutes, not the suggestion: this is the
            # figure the expense of §6.2 step 4 is prefilled with, and prefilling
            # it from a suggestion nobody accepted would pay it by default.
            overtime_amount=(
                overtime_amount(record.overtime_minutes, rate.amount)
                if rate is not None
                else None
            ),
        )
