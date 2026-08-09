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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.core.mixins import SyncableMixin


class OrderStatus(enum.StrEnum):
    """Life cycle of an order (Plan 0001 §7.1).

    The whole set exists from the start even though PR 3 only ever writes
    `received`: an enum type is schema, and adding a value later is a migration
    on a table the app is already mirroring.
    """

    RECEIVED = "received"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


#: Which status may follow which (§7.1). Going back one step is allowed — the
#: diagram draws the return line from every open state — because marking a ticket
#: "en proceso" by mistake happens at a counter and undoing it must not require an
#: administrator. `delivered` and `cancelled` are final: correcting one is voiding
#: and re-capturing (§7.3), not editing.
#:
#: The two transitions that carry data of their own (`delivered`, `cancelled`) are
#: absent from here on purpose: they have their own endpoints, because a delivery
#: needs the garment counts and a cancellation needs a reason.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.RECEIVED: frozenset({OrderStatus.IN_PROGRESS}),
    OrderStatus.IN_PROGRESS: frozenset({OrderStatus.READY, OrderStatus.RECEIVED}),
    OrderStatus.READY: frozenset({OrderStatus.IN_PROGRESS}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

#: Voiding is possible while the laundry still has the clothes and nothing has
#: been handed back.
CANCELLABLE_FROM: frozenset[OrderStatus] = frozenset(
    {OrderStatus.RECEIVED, OrderStatus.IN_PROGRESS}
)

#: Handing the clothes back is possible from any state where the laundry still
#: has them — the chain above is optional bookkeeping, not a gate.
#:
#: The counter marks `in_progress` and `ready` when knowing what is in the wash
#: helps them, and on an ordinary day it does not: the delivery is registered in
#: one pass at closing time, by booklet number, against tickets that never left
#: `received`. Requiring `ready` first would only invent two taps nobody takes,
#: and the workaround — tapping through the chain to unlock the button — would
#: fill the column with timestamps that record the workaround rather than the
#: laundry. What closes a ticket is the delivery itself.
DELIVERABLE_FROM: frozenset[OrderStatus] = frozenset(
    {OrderStatus.RECEIVED, OrderStatus.IN_PROGRESS, OrderStatus.READY}
)


class PaymentMethod(enum.StrEnum):
    CASH = "cash"
    TRANSFER = "transfer"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum *values* (lowercase) instead of member names."""
    return [member.value for member in enum_cls]


class Order(SyncableMixin, Base):
    """A ticket: what came in, what it costs, and who took it."""

    __tablename__ = "orders"
    __table_args__ = (
        # The daily correlative is the number people say out loud ("el 4 de
        # hoy"), so two orders may not share one (D4, D11).
        UniqueConstraint("order_date", "daily_number", name="uq_orders_daily_number"),
        CheckConstraint("daily_number > 0", name="ck_orders_daily_number_positive"),
        # The arithmetic of §6.2 written into the schema. The service is the only
        # thing that computes these, but a total that stops matching its parts is
        # the kind of bug that is found months later, on a ticket someone paid.
        CheckConstraint("total = subtotal - discount_total", name="ck_orders_total_matches"),
        CheckConstraint("discount_total <= subtotal", name="ck_orders_discount_within"),
        # Partial index and not a plain UNIQUE: the printed booklet is optional,
        # and every ticket captured without one would otherwise collide.
        Index(
            "uq_orders_booklet_serial",
            "booklet_serial",
            unique=True,
            postgresql_where=text("booklet_serial IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    #: Business date (D8): what defines the correlative and the daily close.
    #: Without an index of its own: `uq_orders_daily_number` starts with this
    #: column, so every query that filters by date already has one.
    order_date: Mapped[date] = mapped_column(Date)
    daily_number: Mapped[int] = mapped_column(Integer)
    #: Serial printed on the physical ticket by the print shop.
    booklet_serial: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    #: Tax id used on *this* ticket; a customer may bill to a different one.
    nit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    weight_lbs: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    #: Σ of the garment lines. Stored so the list does not have to join to show
    #: it, but never taken from the client (§6.3).
    total_pieces: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", values_callable=_enum_values),
        default=OrderStatus.RECEIVED,
        index=True,
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount_total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    received_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: Indexed for the daily close, which counts the tickets handed back within
    #: a business day (§5.4) — a range over this column and nothing else, so
    #: without the index every preview scans the whole of `orders`, all day.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    delivered_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    garments: Mapped[list[OrderGarment]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    charges: Mapped[list[OrderCharge]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    discounts: Mapped[list[OrderDiscount]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list[OrderPayment]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    @property
    def paid_total(self) -> Decimal:
        return sum(
            (payment.amount for payment in self.payments if payment.deleted_at is None),
            Decimal("0.00"),
        )

    @property
    def balance(self) -> Decimal:
        """What the customer still owes. Derived, never stored (§5.3).

        A stored balance is a second copy of the truth, and the first thing to go
        stale the day a payment is voided.
        """
        return self.total - self.paid_total


class OrderGarment(SyncableMixin, Base):
    """One garment kind on the ticket — the loss-control detail."""

    __tablename__ = "order_garments"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_garments_quantity_positive"),
        # Partial, so a correction can replace the line of a garment kind the
        # ticket keeps: the old row stays as a tombstone until every device has
        # seen it go, and two rows of "Camisa" are only a capture slip while
        # both are live.
        Index(
            "uq_order_garments_type",
            "order_id",
            "garment_type_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    garment_type_id: Mapped[UUID] = mapped_column(ForeignKey("garment_types.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    #: Filled in at delivery; the gap against `quantity` is what went missing.
    quantity_delivered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="garments")


class OrderCharge(SyncableMixin, Base):
    """A billed line with its price frozen at capture time (D2, D3).

    Description and unit price are copies, not lookups: raising the price of the
    large tub tomorrow must not rewrite what yesterday's ticket says it cost.
    """

    __tablename__ = "order_charges"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_charges_quantity_positive"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    service_type_id: Mapped[UUID] = mapped_column(ForeignKey("service_types.id"))
    service_option_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("service_options.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(160))
    #: Pounds, tubs, ten-minute stretches, repetitions of an add-on… (D3.1).
    quantity: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal(1))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="charges")


class OrderDiscount(SyncableMixin, Base):
    """Money taken off the subtotal, with the reason written down."""

    __tablename__ = "order_discounts"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_order_discounts_amount_positive"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    #: `None` means a manual discount, which demands `orders.manual_discount`.
    promotion_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("promotions.id"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(160))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="discounts")


class OrderPayment(SyncableMixin, Base):
    """Money received against a ticket (§5.3).

    Payments accumulate instead of overwriting a `paid` flag: an advance at the
    counter and the rest on delivery are two events, and the day someone disputes
    a charge the question is always *when* and *how much*, not *whether*.
    """

    __tablename__ = "order_payments"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_order_payments_amount_positive"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method", values_callable=_enum_values)
    )
    #: Money taken before the work is done. Rare, but it changes nothing about
    #: how the balance is computed — it is here so the daily close can tell them
    #: apart.
    is_advance: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Transfer number, so a deposit can be found in the bank statement.
    reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    received_by_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: Indexed for the same reason: the drawer is counted by when the money was
    #: handed over, not by the date of the ticket it settles (D1), so the close
    #: cuts this column by range and joins back to the order afterwards.
    paid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="payments")
