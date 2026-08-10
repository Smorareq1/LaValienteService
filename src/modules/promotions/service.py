from __future__ import annotations

from datetime import date
from uuid import UUID

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.catalog.repository import CatalogRepository
from src.modules.promotions.models import DiscountType, Promotion
from src.modules.promotions.repository import PromotionsRepository
from src.modules.promotions.schemas import PromotionCreate, PromotionUpdate


class PromotionsService:
    """Administering promotions: unique codes, real services, sane windows.

    How much a promotion takes off is *not* here — that is the discount engine's
    job, because it needs the ticket's charges to work it out (Plan 0001 §5.4).
    """

    def __init__(self, repository: PromotionsRepository, catalog: CatalogRepository) -> None:
        self.repository = repository
        self.catalog = catalog

    async def list_promotions(
        self, *, active_on: date | None = None, include_inactive: bool = False
    ) -> list[Promotion]:
        if active_on is not None:
            return await self.repository.list_active_on(active_on)
        return await self.repository.list_all(include_inactive=include_inactive)

    async def get(self, promotion_id: UUID) -> Promotion:
        promotion = await self.repository.get(promotion_id)
        if promotion is None:
            raise NotFoundError("Promotion not found.")
        return promotion

    async def create(self, data: PromotionCreate) -> Promotion:
        if await self.repository.get_by_code(data.code) is not None:
            raise ConflictError(f"A promotion with code '{data.code}' already exists.")
        await self._check_service_codes(data.applies_to_service_codes)

        promotion = Promotion(
            code=data.code,
            name=data.name,
            description=data.description,
            discount_type=data.discount_type,
            value=data.value,
            applies_to_service_codes=data.applies_to_service_codes,
            valid_from=data.valid_from,
            valid_to=data.valid_to,
        )
        self.repository.add(promotion)
        await self.repository.commit()
        return promotion

    async def update(self, promotion_id: UUID, data: PromotionUpdate) -> Promotion:
        promotion = await self.get(promotion_id)
        changes = data.model_dump(exclude_unset=True)

        if "applies_to_service_codes" in changes:
            await self._check_service_codes(changes["applies_to_service_codes"])

        for field, value in changes.items():
            setattr(promotion, field, value)

        # Checked after applying, because either half of the pair may be the one
        # changing: raising the value of an existing percentage promotion and
        # turning a fixed amount into a percentage are the same mistake.
        if promotion.discount_type is DiscountType.PERCENTAGE and promotion.value > 100:
            raise ConflictError("A percentage promotion cannot go over 100.")
        if promotion.valid_to is not None and promotion.valid_to < promotion.valid_from:
            raise ConflictError("The promotion cannot end before it starts.")

        await self.repository.commit()
        return promotion

    async def _check_service_codes(self, codes: list[str] | None) -> None:
        """Refuse a promotion aimed at a service that does not exist.

        A typo here is silent otherwise: the promotion saves, appears in the app,
        and takes nothing off — because no line ever matches the code.

        Inactive services count as existing: this asks whether the code is real,
        and a promotion must not become uneditable because a service it names was
        retired.
        """
        if not codes:
            return
        known = {
            service.code
            for service in await self.catalog.list_service_types(include_inactive=True)
        }
        unknown = sorted(set(codes) - known)
        if unknown:
            raise ConflictError(f"These services do not exist: {', '.join(unknown)}.")
