from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.core.mixins import SyncableMixin
from src.modules.orders.models import PaymentMethod

#: `PaymentMethod` above is imported and not redeclared: `payment_method` is one
#: PostgreSQL type, born in the orders migration and reused here (D13). Two Python
#: enums over one database type is how the two halves of the daily close drift
#: apart.


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class MovementType(enum.StrEnum):
    """Why a lot's stock changed (Plan 0005 §5.2).

    The four are created together even though `adjustment` only appears when a
    sale is voided: adding a value to an enum the devices already mirror is a
    migration on live data (same reasoning as `order_status`).
    """

    #: A lot arrived. One per lot, written when the lot is registered.
    PURCHASE_IN = "purchase_in"
    #: Sold over the counter. Carries the price it went out at.
    SALE_OUT = "sale_out"
    #: Consumed by the laundry itself — softener used on the day's orders.
    INTERNAL_USE = "internal_use"
    #: A correction: a count that did not match, or stock coming back from a
    #: voided sale. The only type whose quantity may be negative (§6.3).
    ADJUSTMENT = "adjustment"


#: How each type moves `quantity_available`. `adjustment` is already signed, so
#: it is the identity; the other three are directional by name.
MOVEMENT_SIGN: dict[MovementType, int] = {
    MovementType.PURCHASE_IN: 1,
    MovementType.SALE_OUT: -1,
    MovementType.INTERNAL_USE: -1,
    MovementType.ADJUSTMENT: 1,
}

#: The types a person may record by hand (`POST /inventory/movements`). Purchases
#: come from registering a lot and sales from selling, and letting either be typed
#: in directly would put stock in the kardex with no document behind it.
MANUAL_MOVEMENT_TYPES: frozenset[MovementType] = frozenset(
    {MovementType.INTERNAL_USE, MovementType.ADJUSTMENT}
)


class Product(SyncableMixin, Base):
    """A supply the laundry buys: detergent, softener, bleach (§3)."""

    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    #: What one unit is — bote, bolsa, galón, saco. Free text because the
    #: supplier decides it, not the system.
    unit: Mapped[str] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Path relative to `MEDIA_DIR`, never the bytes (D10). Swapping local disk
    #: for S3 tomorrow is a change of one module, not a migration.
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProductLot(SyncableMixin, Base):
    """One purchase of one product — the "#7" written next to it on the sheet.

    Stock lives here and not on the product because what a bottle cost and what
    it sells for are facts about the batch that arrived, not about the kind of
    thing it is (D3).
    """

    __tablename__ = "product_lots"
    __table_args__ = (
        # The correlative people say out loud ("Suavizante #14"), so it is unique
        # per product and assigned by the system (D3).
        UniqueConstraint("product_id", "lot_number", name="uq_product_lots_number"),
        CheckConstraint("lot_number > 0", name="ck_product_lots_number_positive"),
        CheckConstraint("quantity_received > 0", name="ck_product_lots_received_positive"),
        # Available may exceed received — a count that found more is a legitimate
        # upward adjustment — but it may never go under zero: negative stock is
        # not a state of the shelf, it is a bug that already happened.
        CheckConstraint("quantity_available >= 0", name="ck_product_lots_available_not_negative"),
        CheckConstraint("unit_cost IS NULL OR unit_cost >= 0", name="ck_product_lots_cost_valid"),
        CheckConstraint(
            "sale_price IS NULL OR sale_price >= 0", name="ck_product_lots_price_valid"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # `uq_product_lots_number` is `(product_id, lot_number)`, so it already
    # serves every lookup by product — including the FIFO scan, which is the
    # hottest read here and takes `FOR UPDATE` while it runs.
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    lot_number: Mapped[int] = mapped_column(Integer)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: Cache of the kardex, kept in the same transaction as the movement (D4).
    quantity_available: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: `None` for the lots already on the shelf when this system arrived: "no hay
    #: registro de precios originales" (D3). Every purchase from here on has one.
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    #: `None` means the lot is for the laundry's own use and is not for sale.
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    received_at: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_sellable(self) -> bool:
        return self.sale_price is not None and self.quantity_available > 0


class SupplySale(SyncableMixin, Base):
    """A counter sale of supplies — the blue rows of the sheet (D2).

    A document of its own and never a line on a laundry ticket: the customer who
    buys a bag of detergent may leave no clothes at all, and the daily close adds
    the two incomes separately (§6.1).
    """

    __tablename__ = "supply_sales"
    __table_args__ = (CheckConstraint("total >= 0", name="ck_supply_sales_total_not_negative"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sale_date: Mapped[date] = mapped_column(Date, index=True)
    #: Anonymous counter sales are the common case; a customer is attached only
    #: when someone asks for it or wants the sale on their history.
    customer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customers.id"), nullable=True, index=True
    )
    nit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method", values_callable=_enum_values)
    )
    reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Σ of the lines, always computed by the service and never taken from the
    #: client — the same rule the order total lives under (§6.3 of Plan 0001).
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    sold_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[SupplySaleItem]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


class SupplySaleItem(SyncableMixin, Base):
    """One lot going out on a sale, with its price frozen at the counter.

    `description` and `unit_price` are copies and not lookups, exactly like an
    order charge: repricing the lot tomorrow must not rewrite what the customer
    was charged today (D5, and D2 of Plan 0001).
    """

    __tablename__ = "supply_sale_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_supply_sale_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_supply_sale_items_price_not_negative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sale_id: Mapped[UUID] = mapped_column(
        ForeignKey("supply_sales.id", ondelete="CASCADE"), index=True
    )
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("product_lots.id"), index=True)
    description: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sale: Mapped[SupplySale] = relationship(back_populates="items")


class InventoryMovement(SyncableMixin, Base):
    """One line of the kardex: every change of stock, with its reason (D4).

    Nothing moves `quantity_available` without writing one of these in the same
    transaction, which is what makes the cached number auditable instead of
    merely convenient.
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        # `adjustment` is the one type that may go either way: counting the shelf
        # and finding one bottle fewer is the canonical correction, and routing
        # it through `internal_use` would mix breakage into the number that will
        # one day be the real cost of an order. The other three are directional
        # by name, so a negative one there is a sign error.
        CheckConstraint(
            "CASE WHEN movement_type = 'adjustment' THEN quantity <> 0 ELSE quantity > 0 END",
            name="ck_inventory_movements_quantity_signed",
        ),
        CheckConstraint(
            "unit_price IS NULL OR unit_price >= 0",
            name="ck_inventory_movements_price_not_negative",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("product_lots.id"), index=True)
    movement_type: Mapped[MovementType] = mapped_column(
        Enum(MovementType, name="inventory_movement_type", values_callable=_enum_values),
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    #: Only on `sale_out`: what the unit actually went out at.
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    #: The sale line this movement came from, so a kardex row can be traced back
    #: to the document that caused it.
    supply_sale_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("supply_sale_items.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: Indexed because the kardex is ordered by it. This is the one table of the
    #: phase that grows with every sale and every adjustment and is never cut by
    #: date, so without the index each page is a full scan plus a sort.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    @property
    def stock_delta(self) -> Decimal:
        """How much this movement added to (or took from) the lot."""
        return self.quantity * MOVEMENT_SIGN[self.movement_type]
