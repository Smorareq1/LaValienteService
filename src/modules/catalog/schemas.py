from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.catalog.models import PricingMode

CODE_PATTERN = r"^[a-z][a-z0-9_]*$"
OPTION_CODE_PATTERN = r"^[A-Za-z0-9]{1,20}$"


class ServiceOptionCreate(BaseModel):
    code: str = Field(pattern=OPTION_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    min_quantity: int | None = Field(default=None, ge=0)
    max_quantity: int | None = Field(default=None, ge=0)
    sort_order: int = 0

    @model_validator(mode="after")
    def _check_range(self) -> ServiceOptionCreate:
        if (
            self.min_quantity is not None
            and self.max_quantity is not None
            and self.min_quantity > self.max_quantity
        ):
            raise ValueError("min_quantity cannot be greater than max_quantity.")
        return self


class ServiceOptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    min_quantity: int | None = Field(default=None, ge=0)
    max_quantity: int | None = Field(default=None, ge=0)
    sort_order: int | None = None
    is_active: bool | None = None


class ServiceOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    min_quantity: int | None
    max_quantity: int | None
    is_active: bool
    sort_order: int
    version: int
    #: Price in force on the requested date; `None` when no price covers it.
    current_price: Decimal | None = None


class ServiceTypeCreate(BaseModel):
    code: str = Field(pattern=CODE_PATTERN, max_length=50)
    name: str = Field(min_length=1, max_length=120)
    pricing_mode: PricingMode
    unit_label: str | None = Field(default=None, max_length=30)
    sort_order: int = 0
    options: list[ServiceOptionCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_options(self) -> ServiceTypeCreate:
        if self.pricing_mode is PricingMode.TIERED and not self.options:
            raise ValueError("A tiered service needs at least one option.")
        if self.pricing_mode is not PricingMode.TIERED and self.options:
            raise ValueError("Only tiered services can declare options.")
        return self


class ServiceTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit_label: str | None = Field(default=None, max_length=30)
    sort_order: int | None = None
    is_active: bool | None = None


class ServiceTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    pricing_mode: PricingMode
    unit_label: str | None
    is_active: bool
    sort_order: int
    version: int
    #: Only for `per_unit` services; tiered services price per option.
    current_price: Decimal | None = None
    options: list[ServiceOptionRead] = Field(default_factory=list)


class ServicePriceCreate(BaseModel):
    """A new price window. The service closes the previous one (Plan 0001 §5.2)."""

    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    valid_from: date
    #: Required for tiered services, forbidden otherwise.
    service_option_id: UUID | None = None


class ServicePriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_type_id: UUID
    service_option_id: UUID | None
    price: Decimal
    valid_from: date
    valid_to: date | None


class GarmentTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=1000)
    sort_order: int = 0


class GarmentTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=1000)
    sort_order: int | None = None
    is_active: bool | None = None


class GarmentTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    notes: str | None
    is_active: bool
    sort_order: int
    version: int
