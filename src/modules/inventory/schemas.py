from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.customers.schemas import NIT_PATTERN
from src.modules.inventory.models import MANUAL_MOVEMENT_TYPES, MovementType
from src.modules.orders.models import PaymentMethod

#: Money and quantities share the column type of the whole system (D14), so the
#: schemas share the ceiling too.
MAX_AMOUNT = Decimal("99999999.99")


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: bote, bolsa, galón, saco… free text because the supplier decides it.
    unit: str = Field(min_length=1, max_length=30)
    description: str | None = Field(default=None, max_length=2000)
    sort_order: int = 0


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = Field(default=None, min_length=1, max_length=30)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None
    sort_order: int | None = None


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    unit: str
    description: str | None
    #: Path under `MEDIA_DIR` (D10). It changes whenever the image is replaced,
    #: which is what lets the app tell a new photo from the one it cached.
    image_path: str | None
    is_active: bool
    sort_order: int
    version: int
    #: Σ of what the live lots still hold — derived, never stored (§6.3).
    stock: Decimal
    #: The part of that stock that has a sale price. The rest is the laundry's
    #: own supply and is not on the counter.
    sellable_stock: Decimal
    #: What the next unit sold would cost, which is the oldest sellable lot's
    #: price (D5). `None` when there is nothing to sell.
    next_sale_price: Decimal | None


class LotPurchaseExpense(BaseModel):
    """The money side of a lot arriving (§6.3).

    Sent alongside the lot so the two go in together: the shelf and the day's
    expenses stop agreeing the moment one can be written without the other. It
    carries no concept — that is built from the product and the lot number — and
    no category: a purchase is booked under "Compra de insumos", which is the
    whole point of having the link.
    """

    total: Decimal = Field(gt=0, le=MAX_AMOUNT, decimal_places=2)
    method: PaymentMethod = PaymentMethod.CASH
    #: The sheet's "pago atrasado": the supplier delivered, the money has not
    #: gone out yet, and the day still owes it.
    pending: bool = False
    observations: str | None = Field(default=None, max_length=2000)


class ProductLotCreate(BaseModel):
    """A purchase arriving. The lot number is the system's to assign (D3)."""

    quantity_received: Decimal = Field(gt=0, le=MAX_AMOUNT, decimal_places=2)
    #: What one unit cost. Optional because a lot can be registered from the
    #: shelf without an invoice — and when `expense` is sent and this is not, it
    #: is worked out from the total: that is the "registro de precios originales"
    #: the sheet never had (D3).
    unit_cost: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT, decimal_places=2)
    #: `None` means the laundry keeps this lot for itself; it will never be sold.
    sale_price: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT, decimal_places=2)
    received_at: date | None = None
    #: Goes on the `purchase_in` movement, not on the lot: it is a fact about the
    #: arrival ("factura 3341"), not about the batch.
    notes: str | None = Field(default=None, max_length=2000)
    #: When present, the purchase is booked as an expense of the same day.
    expense: LotPurchaseExpense | None = None


class ProductLotUpdate(BaseModel):
    """Correcting what a lot cost or what it sells for.

    Quantities are deliberately absent: stock moves through the kardex and
    nowhere else (D4). Fixing a miscount is an `adjustment`, which leaves a line
    saying so.
    """

    unit_cost: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT, decimal_places=2)
    sale_price: Decimal | None = Field(default=None, ge=0, le=MAX_AMOUNT, decimal_places=2)


class ProductLotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    lot_number: int
    quantity_received: Decimal
    quantity_available: Decimal
    unit_cost: Decimal | None
    sale_price: Decimal | None
    received_at: date
    version: int


class MovementCreate(BaseModel):
    """A stock change typed in by hand: internal use, or a correction (§6.3)."""

    lot_id: UUID
    movement_type: MovementType
    #: Negative only for `adjustment` — a shelf count that came up short. The
    #: other types carry their direction in their name.
    quantity: Decimal = Field(le=MAX_AMOUNT, ge=-MAX_AMOUNT, decimal_places=2)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_direction(self) -> MovementCreate:
        if self.movement_type not in MANUAL_MOVEMENT_TYPES:
            raise ValueError(
                f"'{self.movement_type}' is written by the system, not by hand: a purchase "
                "comes from registering a lot and a sale from selling one."
            )
        if self.quantity == 0:
            raise ValueError("A movement of zero changes nothing.")
        if self.movement_type is not MovementType.ADJUSTMENT and self.quantity < 0:
            raise ValueError(
                f"'{self.movement_type}' already takes stock out; "
                "send how much, not minus how much."
            )
        return self


class MovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lot_id: UUID
    product_id: UUID
    product_name: str
    lot_number: int
    movement_type: MovementType
    quantity: Decimal
    unit_price: Decimal | None
    supply_sale_item_id: UUID | None
    notes: str | None
    created_by_id: UUID
    created_at: datetime
    #: What this line did to the lot, sign included. The kardex is read as a
    #: running balance and the reader should not have to know the sign table.
    stock_delta: Decimal


class SupplySaleLineCreate(BaseModel):
    """What the seller picks: a product and how many. Never a lot, never a price.

    Which lots cover it is FIFO's answer (D5) and what they cost is the lot's
    own price — the counter chooses neither.
    """

    product_id: UUID
    quantity: Decimal = Field(gt=0, le=MAX_AMOUNT, decimal_places=2)


class SupplySaleCreate(BaseModel):
    #: Generated by the device when the sale is captured offline, so re-sending
    #: the same sale twice is the same sale (Plan 0004).
    id: UUID | None = None
    sale_date: date | None = None
    customer_id: UUID | None = None
    nit: str | None = Field(default=None, pattern=NIT_PATTERN)
    method: PaymentMethod = PaymentMethod.CASH
    reference: str | None = Field(default=None, max_length=80)
    lines: list[SupplySaleLineCreate] = Field(min_length=1)


class SupplySaleCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class SupplySaleItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lot_id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class SupplySaleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sale_date: date
    customer_id: UUID | None
    nit: str | None
    method: PaymentMethod
    reference: str | None
    total: Decimal
    sold_by_id: UUID
    cancelled_at: datetime | None
    cancel_reason: str | None
    version: int
    items: list[SupplySaleItemRead]


class SupplySalePage(BaseModel):
    items: list[SupplySaleRead]
    total: int
    page: int
    page_size: int
