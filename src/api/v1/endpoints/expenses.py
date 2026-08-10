from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import (
    BaseVersion,
    CurrentUser,
    ExpensesServiceDependency,
    require_permission,
)
from src.core.business_time import business_date
from src.modules.expenses.models import ExpenseStatus
from src.modules.expenses.schemas import (
    ExpenseCategoryCreate,
    ExpenseCategoryRead,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseRead,
    ExpensesDaySummary,
    ExpenseUpdate,
    ExpenseVoid,
)

router = APIRouter(prefix="/expenses", tags=["Expenses"])

ExpenseDate = Annotated[date | None, Query(alias="date", description="Business date.")]


# The category routes come first on purpose: `/expenses/{expense_id}` would
# otherwise be tried against the word "categories" and answer 422 instead of
# reaching this.
@router.get(
    "/categories",
    response_model=list[ExpenseCategoryRead],
    dependencies=[Depends(require_permission("expenses.read"))],
)
async def list_categories(
    service: ExpensesServiceDependency, include_inactive: bool = False
) -> list[ExpenseCategoryRead]:
    categories = await service.list_categories(include_inactive=include_inactive)
    return [ExpenseCategoryRead.model_validate(category) for category in categories]


@router.post(
    "/categories",
    response_model=ExpenseCategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("expenses.manage_categories"))],
)
async def create_category(
    data: ExpenseCategoryCreate, service: ExpensesServiceDependency
) -> ExpenseCategoryRead:
    return ExpenseCategoryRead.model_validate(await service.create_category(data))


@router.patch(
    "/categories/{category_id}",
    response_model=ExpenseCategoryRead,
    dependencies=[Depends(require_permission("expenses.manage_categories"))],
)
async def update_category(
    category_id: UUID, data: ExpenseCategoryUpdate, service: ExpensesServiceDependency
) -> ExpenseCategoryRead:
    return ExpenseCategoryRead.model_validate(await service.update_category(category_id, data))


@router.get(
    "/summary",
    response_model=ExpensesDaySummary,
    dependencies=[Depends(require_permission("expenses.read"))],
)
async def day_summary(
    service: ExpensesServiceDependency, expense_date: ExpenseDate = None
) -> ExpensesDaySummary:
    """The day's expenses added up — the right-hand total of the sheet (§6.1).

    Not in the plan's table either: the daily close of PR 10 will carry these
    numbers, but the Caja screen needs them while the day is still open, and
    adding up a list the client already has is how two totals start to differ.
    """
    return await service.day_summary(expense_date or business_date())


@router.get(
    "",
    response_model=list[ExpenseRead],
    dependencies=[Depends(require_permission("expenses.read"))],
)
async def list_expenses(
    service: ExpensesServiceDependency,
    expense_date: ExpenseDate = None,
    category_id: UUID | None = None,
    expense_status: Annotated[ExpenseStatus | None, Query(alias="status")] = None,
    employee_id: UUID | None = None,
) -> list[ExpenseRead]:
    return await service.list_expenses(
        expense_date=expense_date,
        category_id=category_id,
        status=expense_status,
        employee_id=employee_id,
    )


@router.post(
    "",
    response_model=ExpenseRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("expenses.create"))],
)
async def create_expense(
    data: ExpenseCreate, service: ExpensesServiceDependency, user: CurrentUser
) -> ExpenseRead:
    """Record money going out.

    Overtime is an ordinary expense with two links on it (§6.2 step 4): the
    employee and the working day. The amount to send is the `overtime_amount`
    that `GET /staff/attendance` already worked out for the confirmed minutes —
    the server suggests it there, a person accepts it here, and the same day
    cannot be paid twice.
    """
    return await service.create_expense(data, actor=user)


@router.patch(
    "/{expense_id}",
    response_model=ExpenseRead,
    dependencies=[Depends(require_permission("expenses.update"))],
)
async def update_expense(
    expense_id: UUID,
    data: ExpenseUpdate,
    service: ExpensesServiceDependency,
    base_version: BaseVersion = None,
) -> ExpenseRead:
    """Correct an expense. The links it carries are not editable — see the service."""
    return await service.update_expense(expense_id, data, base_version=base_version)


@router.delete(
    "/{expense_id}",
    response_model=ExpenseRead,
    dependencies=[Depends(require_permission("expenses.void"))],
)
async def void_expense(
    expense_id: UUID,
    data: ExpenseVoid,
    service: ExpensesServiceDependency,
    user: CurrentUser,
) -> ExpenseRead:
    """Take an expense off the day, with a name and a reason on it.

    A tombstone rather than a delete: a device that already pulled the row has
    to learn it is gone (Plan 0004 D8), and the day's total has to stay
    explainable to whoever counted the drawer.
    """
    return await service.void_expense(expense_id, data, actor=user)
