from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.promotions.models import DiscountType

CODE_PATTERN = r"^[a-z][a-z0-9_]*$"


def _check_percentage(discount_type: DiscountType, value: Decimal) -> None:
    if discount_type is DiscountType.PERCENTAGE and value > 100:
        raise ValueError("A percentage promotion cannot go over 100.")


class PromotionCreate(BaseModel):
    code: str = Field(pattern=CODE_PATTERN, max_length=50)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    discount_type: DiscountType
    value: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    #: Service codes the promotion applies to; omitted means the whole ticket.
    applies_to_service_codes: list[str] | None = Field(default=None, min_length=1)
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def _check(self) -> PromotionCreate:
        _check_percentage(self.discount_type, self.value)
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("The promotion cannot end before it starts.")
        return self


class PromotionUpdate(BaseModel):
    """Everything but the code, which is what orders already point at.

    Amount and type stay editable on purpose: a promotion is corrected before it
    is used, and the tickets already taken keep their own snapshot of what came
    off (D2), so changing it here cannot rewrite them.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    discount_type: DiscountType | None = None
    value: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    applies_to_service_codes: list[str] | None = Field(default=None, min_length=1)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None


class PromotionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    description: str | None
    discount_type: DiscountType
    value: Decimal
    applies_to_service_codes: list[str] | None
    valid_from: date
    valid_to: date | None
    is_active: bool
    version: int
