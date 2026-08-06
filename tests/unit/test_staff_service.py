"""The rules around a worked day (Plan 0005 §6.2, D6 to D8).

In-memory repository: these are decisions, not queries. What the module must get
right is that the system *suggests* overtime and a person *confirms* it, and that
raising the rate never rewrites what was already paid.
"""

from datetime import date, time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.core.mixins import SyncableMixin
from src.modules.staff.models import (
    OVERTIME_RATE_CODE,
    AttendanceRecord,
    Employee,
    PayrollRate,
    WorkShift,
)
from src.modules.staff.schemas import (
    AttendanceCreate,
    AttendanceUpdate,
    EmployeeCreate,
    EmployeeUpdate,
    PayrollRateCreate,
    WorkShiftUpdate,
)
from src.modules.staff.service import StaffService

WORK_DATE = date(2026, 7, 18)


def employee(
    name: str = "Claudia", *, active: bool = True, user_id: UUID | None = None
) -> Employee:
    person = Employee(full_name=name, is_active=active, user_id=user_id)
    person.id = uuid4()
    person.version = 1
    return person


def morning_shift() -> WorkShift:
    shift = WorkShift(
        code="T1", name="Turno mañana", starts_at=time(7, 0), ends_at=time(12, 0), sort_order=1
    )
    shift.id = uuid4()
    shift.version = 1
    return shift


def rate(amount: str, valid_from: date, valid_to: date | None = None) -> PayrollRate:
    window = PayrollRate(
        code=OVERTIME_RATE_CODE,
        amount=Decimal(amount),
        valid_from=valid_from,
        valid_to=valid_to,
    )
    window.id = uuid4()
    window.version = 1
    return window


class FakeStaffRepository:
    """Stands in for the database, including what the database fills in.

    `add` assigns the id and the version because both are column defaults that
    only fire on write, and the read schemas need them the moment the service
    finishes: a fake that leaves them empty would fail for a reason that has
    nothing to do with the rule under test.
    """

    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        shifts: list[WorkShift] | None = None,
        rates: list[PayrollRate] | None = None,
        attendance: list[AttendanceRecord] | None = None,
        users: set[UUID] | None = None,
    ) -> None:
        self.employees = employees or []
        self.shifts = shifts or []
        self.rates = rates or []
        self.attendance = attendance or []
        self.users = users or set()
        self.committed = False

    async def list_employees(self, *, include_inactive: bool = False) -> list[Employee]:
        return [person for person in self.employees if include_inactive or person.is_active]

    async def get_employee(self, employee_id: UUID) -> Employee | None:
        return next((person for person in self.employees if person.id == employee_id), None)

    async def get_employee_by_user(self, user_id: UUID) -> Employee | None:
        return next((person for person in self.employees if person.user_id == user_id), None)

    async def user_exists(self, user_id: UUID) -> bool:
        return user_id in self.users

    async def list_shifts(self, *, include_inactive: bool = False) -> list[WorkShift]:
        return [shift for shift in self.shifts if include_inactive or shift.is_active]

    async def get_shift(self, shift_id: UUID) -> WorkShift | None:
        return next((shift for shift in self.shifts if shift.id == shift_id), None)

    async def get_shift_by_code(self, code: str) -> WorkShift | None:
        return next((shift for shift in self.shifts if shift.code == code), None)

    async def list_rates(self, code: str | None = None) -> list[PayrollRate]:
        return [window for window in self.rates if code is None or window.code == code]

    async def get_open_rate(self, code: str) -> PayrollRate | None:
        return next(
            (
                window
                for window in self.rates
                if window.code == code and window.valid_to is None
            ),
            None,
        )

    async def get_rate_on(self, code: str, on_date: date) -> PayrollRate | None:
        covering = [
            window
            for window in self.rates
            if window.code == code and window.covers(on_date)
        ]
        return max(covering, key=lambda window: window.valid_from) if covering else None

    async def list_attendance(
        self, *, work_date: date | None = None, employee_id: UUID | None = None
    ) -> list[AttendanceRecord]:
        return [
            record
            for record in self.attendance
            if (work_date is None or record.work_date == work_date)
            and (employee_id is None or record.employee_id == employee_id)
        ]

    async def get_attendance(self, record_id: UUID) -> AttendanceRecord | None:
        return next((record for record in self.attendance if record.id == record_id), None)

    async def get_open_attendance(
        self, employee_id: UUID, work_date: date
    ) -> AttendanceRecord | None:
        return next(
            (
                record
                for record in self.attendance
                if record.employee_id == employee_id
                and record.work_date == work_date
                and record.clock_out is None
            ),
            None,
        )

    def add(self, instance: Any) -> None:
        if isinstance(instance, SyncableMixin):
            instance.version = 1
        # An id already on the row is kept, the way the database keeps it: a
        # working day clocked in offline arrives with the id the device minted,
        # and overwriting it here would hide exactly the bug it guards against.
        if instance.id is None:
            instance.id = uuid4()
        if isinstance(instance, AttendanceRecord):
            self.attendance.append(instance)
        elif isinstance(instance, Employee):
            self.employees.append(instance)
        elif isinstance(instance, WorkShift):
            self.shifts.append(instance)
        elif isinstance(instance, PayrollRate):
            self.rates.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def service_with(repository: FakeStaffRepository) -> StaffService:
    return StaffService(repository)  # type: ignore[arg-type]


class TestClockIn:
    async def test_records_the_day(self) -> None:
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(
            employees=[person], shifts=[shift], rates=[rate("20.00", date(2026, 1, 1))]
        )

        read = await service_with(repository).clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )

        assert read.employee_name == "Claudia"
        assert read.worked_minutes is None
        assert repository.committed is True

    async def test_refuses_a_second_open_day(self) -> None:
        """Tapping "entrada" twice instead of "salida" — the everyday slip."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )

        with pytest.raises(ConflictError):
            await service.clock_in(
                AttendanceCreate(
                    employee_id=person.id,
                    work_date=WORK_DATE,
                    shift_id=shift.id,
                    clock_in=time(7, 5),
                )
            )

    async def test_a_closed_day_does_not_block_coming_back(self) -> None:
        """Someone who left at noon and returned in the evening works twice."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(7, 0),
                clock_out=time(12, 0),
            )
        )

        second = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id, work_date=WORK_DATE, clock_in=time(18, 0)
            )
        )

        assert second.shift_id is None

    async def test_refuses_someone_who_no_longer_works_here(self) -> None:
        person = employee(active=False)
        repository = FakeStaffRepository(employees=[person])

        with pytest.raises(ConflictError):
            await service_with(repository).clock_in(
                AttendanceCreate(
                    employee_id=person.id, work_date=WORK_DATE, clock_in=time(7, 0)
                )
            )

    async def test_refuses_a_shift_that_does_not_exist(self) -> None:
        person = employee()
        repository = FakeStaffRepository(employees=[person])

        with pytest.raises(NotFoundError):
            await service_with(repository).clock_in(
                AttendanceCreate(
                    employee_id=person.id,
                    work_date=WORK_DATE,
                    shift_id=uuid4(),
                    clock_in=time(7, 0),
                )
            )


class TestCapturedWithoutSignal:
    """What a working day written offline needs to survive the trip (Plan 0005 §6.4)."""

    async def test_the_day_keeps_the_id_the_device_minted(self) -> None:
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        offline_id = uuid4()

        read = await service_with(repository).clock_in(
            AttendanceCreate(
                id=offline_id,
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )

        assert read.id == offline_id

    async def test_re_sending_the_same_day_does_not_open_a_second_one(self) -> None:
        """A push whose answer never made it back is retried, and must be free (D4).

        Without the id the device minted, the retry would look like a person
        clocking in twice — which is a different refusal, and one that leaves the
        device with no way to learn its first attempt had worked.
        """
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        offline_id = uuid4()
        captured = AttendanceCreate(
            id=offline_id,
            employee_id=person.id,
            work_date=WORK_DATE,
            shift_id=shift.id,
            clock_in=time(6, 50),
            clock_out=time(13, 0),
        )
        await service.clock_in(captured)

        with pytest.raises(ConflictError, match="already registered"):
            await service.clock_in(captured)

        assert len(repository.attendance) == 1

    async def test_clocking_out_over_someone_elses_edit_is_a_version_conflict(self) -> None:
        """Two devices closing the same day: the second one never saw the first."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )
        stored = await repository.get_attendance(record.id)
        assert stored is not None
        stored.version = 2

        with pytest.raises(StaleVersionError):
            await service.update_attendance(
                record.id, AttendanceUpdate(clock_out=time(13, 0)), base_version=1
            )

    async def test_an_edit_without_a_version_still_goes_through(self) -> None:
        """The admin screen is not doing optimistic concurrency."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )

        read = await service.update_attendance(record.id, AttendanceUpdate(clock_out=time(13, 0)))

        assert read.clock_out == time(13, 0)


class TestClockOut:
    async def test_suggests_the_extra_minutes_without_confirming_them(self) -> None:
        """The heart of D8: leaving late is not the same as being paid for it."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(
            employees=[person], shifts=[shift], rates=[rate("20.00", date(2026, 1, 1))]
        )
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(6, 50),
            )
        )

        read = await service.update_attendance(
            record.id, AttendanceUpdate(clock_out=time(13, 0))
        )

        assert read.worked_minutes == 370
        assert read.suggested_overtime_minutes == 70
        assert read.overtime_minutes == 0
        assert read.overtime_amount == Decimal("0.00")

    async def test_confirming_the_minutes_prices_them(self) -> None:
        """Half an hour confirmed at Q20 is the Q10 of the paper sheet."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(
            employees=[person], shifts=[shift], rates=[rate("20.00", date(2026, 1, 1))]
        )
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(7, 0),
            )
        )

        read = await service.update_attendance(
            record.id, AttendanceUpdate(clock_out=time(13, 0), overtime_minutes=30)
        )

        assert read.suggested_overtime_minutes == 60
        # Sixty suggested, thirty agreed: the record keeps the decision.
        assert read.overtime_minutes == 30
        assert read.overtime_amount == Decimal("10.00")

    async def test_prices_at_the_rate_of_the_day_worked(self) -> None:
        """A raise in August must not reprice a day worked in July (D7)."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(
            employees=[person],
            shifts=[shift],
            rates=[
                rate("20.00", date(2026, 1, 1), date(2026, 7, 31)),
                rate("25.00", date(2026, 8, 1)),
            ],
        )
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(7, 0),
                clock_out=time(13, 0),
                overtime_minutes=60,
            )
        )

        assert record.overtime_amount == Decimal("20.00")

    async def test_without_a_rate_the_amount_is_unknown_not_zero(self) -> None:
        """A gap in the rate history is a gap, not a free hour."""
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift], rates=[])
        service = service_with(repository)

        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(7, 0),
                clock_out=time(13, 0),
                overtime_minutes=60,
            )
        )

        assert record.overtime_amount is None

    async def test_refuses_an_exit_before_the_entry(self) -> None:
        person, shift = employee(), morning_shift()
        repository = FakeStaffRepository(employees=[person], shifts=[shift])
        service = service_with(repository)
        record = await service.clock_in(
            AttendanceCreate(
                employee_id=person.id,
                work_date=WORK_DATE,
                shift_id=shift.id,
                clock_in=time(7, 0),
            )
        )

        with pytest.raises(ConflictError):
            await service.update_attendance(record.id, AttendanceUpdate(clock_out=time(6, 0)))


class TestRates:
    async def test_a_new_rate_closes_the_one_in_force(self) -> None:
        current = rate("20.00", date(2026, 1, 1))
        repository = FakeStaffRepository(rates=[current])

        created = await service_with(repository).register_rate(
            PayrollRateCreate(
                code=OVERTIME_RATE_CODE, amount=Decimal("25.00"), valid_from=date(2026, 8, 1)
            )
        )

        # No gap, no overlap: the old window ends the day before the new starts.
        assert current.valid_to == date(2026, 7, 31)
        assert created.valid_to is None

    async def test_the_first_rate_needs_no_previous_window(self) -> None:
        repository = FakeStaffRepository(rates=[])

        created = await service_with(repository).register_rate(
            PayrollRateCreate(
                code=OVERTIME_RATE_CODE, amount=Decimal("20.00"), valid_from=date(2026, 1, 1)
            )
        )

        assert created.amount == Decimal("20.00")

    async def test_a_rate_cannot_start_before_the_one_in_force(self) -> None:
        """Backdating would leave two windows covering the same worked day."""
        repository = FakeStaffRepository(rates=[rate("20.00", date(2026, 7, 1))])

        with pytest.raises(ConflictError):
            await service_with(repository).register_rate(
                PayrollRateCreate(
                    code=OVERTIME_RATE_CODE,
                    amount=Decimal("25.00"),
                    valid_from=date(2026, 6, 1),
                )
            )


class TestShifts:
    async def test_moving_the_end_before_the_start_is_refused(self) -> None:
        """Either half may be the one moving, so it is checked after applying."""
        shift = morning_shift()
        repository = FakeStaffRepository(shifts=[shift])

        with pytest.raises(ConflictError):
            await service_with(repository).update_shift(
                shift.id, WorkShiftUpdate(ends_at=time(6, 0))
            )


class TestEmployees:
    async def test_links_an_account(self) -> None:
        user_id = uuid4()
        repository = FakeStaffRepository(users={user_id})

        created = await service_with(repository).create_employee(
            EmployeeCreate(full_name="Claudia", user_id=user_id)
        )

        assert created.user_id == user_id

    async def test_refuses_an_account_that_does_not_exist(self) -> None:
        repository = FakeStaffRepository(users=set())

        with pytest.raises(NotFoundError):
            await service_with(repository).create_employee(
                EmployeeCreate(full_name="Claudia", user_id=uuid4())
            )

    async def test_an_account_belongs_to_one_employee(self) -> None:
        """Two people sharing a login would file one person's hours under the other."""
        user_id = uuid4()
        repository = FakeStaffRepository(
            employees=[employee("Claudia", user_id=user_id)], users={user_id}
        )

        with pytest.raises(ConflictError):
            await service_with(repository).create_employee(
                EmployeeCreate(full_name="Marta", user_id=user_id)
            )

    async def test_keeping_its_own_account_is_not_a_clash(self) -> None:
        user_id = uuid4()
        person = employee("Claudia", user_id=user_id)
        repository = FakeStaffRepository(employees=[person], users={user_id})

        updated = await service_with(repository).update_employee(
            person.id, EmployeeUpdate(phone="5555-5555", user_id=user_id)
        )

        assert updated.phone == "5555-5555"

    async def test_deactivating_does_not_tombstone_the_row(self) -> None:
        """Worked days point at this row; a device that dropped it would show
        last month's attendance with no name on it."""
        person = employee()
        repository = FakeStaffRepository(employees=[person])

        updated = await service_with(repository).update_employee(
            person.id, EmployeeUpdate(is_active=False)
        )

        assert updated.is_active is False
        assert updated.deleted_at is None
