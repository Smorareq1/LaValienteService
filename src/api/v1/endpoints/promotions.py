from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from src.api.dependencies import PromotionsServiceDependency, require_permission
from src.modules.promotions.schemas import PromotionCreate, PromotionRead, PromotionUpdate

router = APIRouter(prefix="/promotions", tags=["Promotions"])

ActiveOn = Annotated[
    date | None,
    Query(description="Only the promotions in force on this business date."),
]


@router.get(
    "",
    response_model=list[PromotionRead],
    dependencies=[Depends(require_permission("promotions.read"))],
)
async def list_promotions(
    service: PromotionsServiceDependency,
    active_on: ActiveOn = None,
    include_inactive: bool = False,
) -> list[PromotionRead]:
    """The promotions to offer, or the whole set to administer.

    `active_on` is what the order screen asks for — the chips it may show — and
    it wins over `include_inactive`: a promotion that is switched off is not in
    force on any date.
    """
    promotions = await service.list_promotions(
        active_on=active_on, include_inactive=include_inactive
    )
    return [PromotionRead.model_validate(promotion) for promotion in promotions]


@router.post(
    "",
    response_model=PromotionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("promotions.manage"))],
)
async def create_promotion(
    data: PromotionCreate, service: PromotionsServiceDependency
) -> PromotionRead:
    promotion = await service.create(data)
    return PromotionRead.model_validate(promotion)


@router.patch(
    "/{promotion_id}",
    response_model=PromotionRead,
    dependencies=[Depends(require_permission("promotions.manage"))],
)
async def update_promotion(
    promotion_id: UUID,
    data: PromotionUpdate,
    service: PromotionsServiceDependency,
) -> PromotionRead:
    """Edit a promotion. Orders already taken keep their own snapshot (D2)."""
    promotion = await service.update(promotion_id, data)
    return PromotionRead.model_validate(promotion)
