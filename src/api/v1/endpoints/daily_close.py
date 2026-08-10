from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import CurrentUser, DailyCloseServiceDependency, require_permission
from src.core.business_time import business_date
from src.modules.daily_close.schemas import (
    DailyCloseCreate,
    DailyClosePreview,
    DailyCloseReopen,
    DailyClosureRead,
)

router = APIRouter(prefix="/daily-close", tags=["Daily close"])

#: How far back the history reaches when no range is asked for.
DEFAULT_HISTORY_DAYS = 30


@router.get(
    "/preview",
    response_model=DailyClosePreview,
    dependencies=[Depends(require_permission("daily_close.read"))],
)
async def preview(
    service: DailyCloseServiceDependency,
    close_date: Annotated[date | None, Query(alias="date", description="Business date.")] = None,
) -> DailyClosePreview:
    """The paper sheet on screen: what the day is worth right now (§6.1).

    Live all day and still live after the close — behind the lock the figures
    cannot move, so the preview agreeing with the filed acta is something a
    person can check rather than take on faith.
    """
    return await service.preview(close_date or business_date())


@router.get(
    "",
    response_model=list[DailyClosureRead],
    dependencies=[Depends(require_permission("daily_close.read"))],
)
async def history(
    service: DailyCloseServiceDependency,
    since: Annotated[date | None, Query(alias="from")] = None,
    until: Annotated[date | None, Query(alias="to")] = None,
) -> list[DailyClosureRead]:
    """The closes on record, reopened ones included — that is the trail (D9)."""
    end = until or business_date()
    return await service.history(
        since=since or end - timedelta(days=DEFAULT_HISTORY_DAYS), until=end
    )


@router.post(
    "",
    response_model=DailyClosureRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("daily_close.close"))],
)
async def close_day(
    data: DailyCloseCreate, service: DailyCloseServiceDependency, user: CurrentUser
) -> DailyClosureRead:
    """File the day and lock the date (D9).

    From here on, tickets, payments, expenses and supply sales of that date are
    refused — by this API and by anything arriving from a device, since the rule
    lives in the services and not at the door.
    """
    return await service.close(data, actor=user)


@router.post(
    "/{closure_id}/reopen",
    response_model=DailyClosureRead,
    dependencies=[Depends(require_permission("daily_close.reopen"))],
)
async def reopen_day(
    closure_id: UUID,
    data: DailyCloseReopen,
    service: DailyCloseServiceDependency,
    user: CurrentUser,
) -> DailyClosureRead:
    """Lift the lock, leaving behind who lifted it and why.

    The day then has to be closed again: the preview says so among its warnings
    until somebody does.
    """
    return await service.reopen(closure_id, data, actor=user)
