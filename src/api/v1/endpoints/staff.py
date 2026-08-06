from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import BaseVersion, StaffServiceDependency, require_permission
from src.modules.staff.schemas import (
    AttendanceCreate,
    AttendanceRead,
    AttendanceUpdate,
    EmployeeCreate,
    EmployeeRead,
    EmployeeUpdate,
    PayrollRateCreate,
    PayrollRateRead,
    WorkShiftCreate,
    WorkShiftRead,
    WorkShiftUpdate,
)

router = APIRouter(prefix="/staff", tags=["Staff"])

RateCode = Annotated[
    str | None,
    Query(description="Only the windows of this rate, e.g. `overtime_hour`."),
]
WorkDate = Annotated[
    date | None,
    Query(description="Business date of the working day."),
]


@router.get(
    "/employees",
    response_model=list[EmployeeRead],
    dependencies=[Depends(require_permission("staff.read"))],
)
async def list_employees(
    service: StaffServiceDependency, include_inactive: bool = False
) -> list[EmployeeRead]:
    """Who works here. A collaborator needs this to clock anyone in (§9.1)."""
    employees = await service.list_employees(include_inactive=include_inactive)
    return [EmployeeRead.model_validate(employee) for employee in employees]


@router.post(
    "/employees",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def create_employee(
    data: EmployeeCreate, service: StaffServiceDependency
) -> EmployeeRead:
    employee = await service.create_employee(data)
    return EmployeeRead.model_validate(employee)


@router.patch(
    "/employees/{employee_id}",
    response_model=EmployeeRead,
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def update_employee(
    employee_id: UUID, data: EmployeeUpdate, service: StaffServiceDependency
) -> EmployeeRead:
    """Edit someone, or take them off the day's list with `is_active: false`.

    Deactivating keeps the row readable: the attendance and the overtime already
    paid point at it.
    """
    employee = await service.update_employee(employee_id, data)
    return EmployeeRead.model_validate(employee)


@router.get(
    "/shifts",
    response_model=list[WorkShiftRead],
    dependencies=[Depends(require_permission("staff.read"))],
)
async def list_shifts(
    service: StaffServiceDependency, include_inactive: bool = False
) -> list[WorkShiftRead]:
    shifts = await service.list_shifts(include_inactive=include_inactive)
    return [WorkShiftRead.model_validate(shift) for shift in shifts]


@router.post(
    "/shifts",
    response_model=WorkShiftRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def create_shift(data: WorkShiftCreate, service: StaffServiceDependency) -> WorkShiftRead:
    shift = await service.create_shift(data)
    return WorkShiftRead.model_validate(shift)


@router.patch(
    "/shifts/{shift_id}",
    response_model=WorkShiftRead,
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def update_shift(
    shift_id: UUID, data: WorkShiftUpdate, service: StaffServiceDependency
) -> WorkShiftRead:
    shift = await service.update_shift(shift_id, data)
    return WorkShiftRead.model_validate(shift)


@router.get(
    "/rates",
    response_model=list[PayrollRateRead],
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def list_rates(
    service: StaffServiceDependency, code: RateCode = None
) -> list[PayrollRateRead]:
    """Every window, newest first — the history the settings screen shows (§9.3)."""
    rates = await service.list_rates(code)
    return [PayrollRateRead.model_validate(rate) for rate in rates]


@router.post(
    "/rates",
    response_model=PayrollRateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("staff.manage"))],
)
async def register_rate(
    data: PayrollRateCreate, service: StaffServiceDependency
) -> PayrollRateRead:
    """Open a new rate window. The one in force ends the day before (D7).

    Overtime already paid keeps its own amount: the expense froze it (§6.2).
    """
    rate = await service.register_rate(data)
    return PayrollRateRead.model_validate(rate)


@router.get(
    "/attendance",
    response_model=list[AttendanceRead],
    dependencies=[Depends(require_permission("staff.read"))],
)
async def list_attendance(
    service: StaffServiceDependency,
    work_date: WorkDate = None,
    employee_id: UUID | None = None,
) -> list[AttendanceRead]:
    """The worked days, with the overtime the system suggests for each one."""
    return await service.list_attendance(work_date=work_date, employee_id=employee_id)


@router.post(
    "/attendance",
    response_model=AttendanceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("attendance.record"))],
)
async def clock_in(data: AttendanceCreate, service: StaffServiceDependency) -> AttendanceRead:
    """Clock someone in, or write down a whole day off the paper sheet."""
    return await service.clock_in(data)


@router.patch(
    "/attendance/{record_id}",
    response_model=AttendanceRead,
    dependencies=[Depends(require_permission("attendance.record"))],
)
async def update_attendance(
    record_id: UUID,
    data: AttendanceUpdate,
    service: StaffServiceDependency,
    base_version: BaseVersion = None,
) -> AttendanceRead:
    """Clock out, or confirm the extra minutes to pay.

    `suggested_overtime_minutes` in the response is what the system worked out;
    `overtime_minutes` is what a person decided. Only the second one is paid (D8).
    """
    return await service.update_attendance(record_id, data, base_version=base_version)
