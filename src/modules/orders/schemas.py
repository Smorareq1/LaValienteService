from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.customers.schemas import NIT_PATTERN, CustomerCreate
from src.modules.orders.models import OrderStatus, PaymentMethod


class OrderGarmentCreate(BaseModel):
    garment_type_id: UUID
    quantity: int = Field(gt=0, le=999)
    notes: str | None = Field(default=None, max_length=500)


class OrderChargeCreate(BaseModel):
    """A line as captured. Prices are never sent (D5) — except `variable` ones."""

    service_code: str = Field(min_length=1, max_length=50)
    option_code: str | None = Field(default=None, max_length=20)
    #: Default 1, but every service accepts N: three softeners is one line (D3.1).
    quantity: Decimal = Field(default=Decimal(1), gt=0, le=Decimal("9999.99"))
    #: Only for `variable` services (pickup/delivery): what the courier charged.
    amount: Decimal | None = Field(default=None, ge=0, le=Decimal("99999999.99"))


class OrderDiscountCreate(BaseModel):
    """Money off: either a promotion, or an amount an administrator typed.

    A promotion sends only its code — the server works out how much it takes off,
    the same way it does with prices (D5). A manual discount sends both, and
    demands `orders.manual_discount`.
    """

    promotion_code: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = Field(default=None, min_length=2, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("99999999.99"))

    @model_validator(mode="after")
    def _is_one_or_the_other(self) -> "OrderDiscountCreate":
        if self.promotion_code is not None:
            if self.description is not None or self.amount is not None:
                raise ValueError(
                    "A promotion carries no amount of its own: the server works it out."
                )
            return self
        if self.description is None or self.amount is None:
            raise ValueError(
                "Send a promotion_code, or a description and an amount for a manual discount."
            )
        return self

    @property
    def is_manual(self) -> bool:
        return self.promotion_code is None


class OrderPaymentCreate(BaseModel):
    """Money handed over. `is_advance` is forced on when it rides on the capture."""

    amount: Decimal = Field(gt=0, le=Decimal("99999999.99"))
    method: PaymentMethod = PaymentMethod.CASH
    is_advance: bool = False
    reference: str | None = Field(default=None, max_length=80)
    #: Minted by the device, like an order's, so a receipt printed offline names
    #: the same payment the server will end up storing (Plan 0004 D3).
    id: UUID | None = None


class OrderPaymentPush(OrderPaymentCreate):
    """A payment arriving from a device, which has to name the ticket it settles.

    Over HTTP the order is in the path; a sync operation has no path, only a
    payload, so it carries the reference itself.
    """

    order_id: UUID


class OrderCreate(BaseModel):
    #: Omitted means today in Guatemala (D8), which is what the counter means.
    order_date: date | None = None
    booklet_serial: str | None = Field(default=None, max_length=20)
    customer_id: UUID | None = None
    #: An unknown customer is registered along with the ticket: the counter
    #: cannot stop to visit another screen while someone waits with the laundry.
    customer: CustomerCreate | None = None
    nit: str | None = Field(default=None, pattern=NIT_PATTERN)
    weight_lbs: Decimal | None = Field(default=None, gt=0, le=Decimal("9999.99"))
    observations: str | None = Field(default=None, max_length=2000)
    garments: list[OrderGarmentCreate] = Field(default_factory=list)
    charges: list[OrderChargeCreate] = Field(min_length=1)
    discounts: list[OrderDiscountCreate] = Field(default_factory=list)
    #: Money taken at the counter, before the work is done (§6.1).
    advance_payment: OrderPaymentCreate | None = None
    #: Devices mint ids offline so a ticket can be printed before it has ever
    #: reached the server (Plan 0004 D3).
    id: UUID | None = None
    #: The reading this ticket was captured from, if any (Plan 0003 §8, D7).
    #:
    #: **`OrdersService` never looks at it.** It is consumed one level up — by
    #: the endpoint and by the sync handler — which then asks `intake_scan` to
    #: record the diff between what the model proposed and what the person
    #: actually saved. That keeps the direction of D2 intact: this module knows
    #: the field exists and nothing about what reads it, and the day the paper
    #: booklet is dropped, retiring the scan module is deleting these three
    #: lines along with it.
    scan_id: UUID | None = None

    @model_validator(mode="after")
    def _needs_exactly_one_customer(self) -> "OrderCreate":
        if (self.customer_id is None) == (self.customer is None):
            raise ValueError("Send either customer_id or customer, not both and not neither.")
        return self


class OrderUpdate(BaseModel):
    """Correct a ticket that is still in the shop (§7.3).

    It **replaces**: what arrives is the ticket as it should read, and the server
    recalculates it whole. Editing line by line would need ids the counter never
    sees, and a boleta is corrected by rewriting it.

    Three things are deliberately absent. The **date and the daily number**, which
    belong to the day the ticket was taken and are what everyone calls it by.
    The **status**, which has its own endpoints because each transition needs
    data this body has no room for. And the **payments**: money received is an
    event that happened, not a field — a payment is voided, never edited away.
    """

    booklet_serial: str | None = Field(default=None, max_length=20)
    #: Omitted keeps the customer the ticket already has.
    customer_id: UUID | None = None
    nit: str | None = Field(default=None, pattern=NIT_PATTERN)
    weight_lbs: Decimal | None = Field(default=None, gt=0, le=Decimal("9999.99"))
    observations: str | None = Field(default=None, max_length=2000)
    garments: list[OrderGarmentCreate] = Field(default_factory=list)
    charges: list[OrderChargeCreate] = Field(min_length=1)
    discounts: list[OrderDiscountCreate] = Field(default_factory=list)
    #: The version the edit was built on (Plan 0004 D6). Over HTTP it is optional
    #: — a screen that just read the ticket is editing what it saw — but a device
    #: that was offline sends it, and a mismatch is a conflict rather than a
    #: silent overwrite of whatever changed meanwhile.
    base_version: int | None = Field(default=None, ge=1)


class OrderStatusChange(BaseModel):
    """Move the ticket along the chain of §7.1.

    Delivery and cancellation are not reachable from here: each has an endpoint of
    its own, because each needs data this body has no room for.
    """

    status: OrderStatus


class OrderDeliverGarment(BaseModel):
    garment_type_id: UUID
    quantity_delivered: int = Field(ge=0, le=999)


class OrderDeliver(BaseModel):
    """Hand the laundry back (§7.2).

    Both fields are optional and mean different things when absent: no garment
    list means everything received is going back, and no payment means the ticket
    was already settled.
    """

    garments: list[OrderDeliverGarment] = Field(default_factory=list)
    payment: OrderPaymentCreate | None = None


class OrderCancel(BaseModel):
    #: Mandatory: a voided ticket with no reason is one nobody can account for.
    reason: str = Field(min_length=3, max_length=500)


class OrderGarmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    garment_type_id: UUID
    quantity: int
    quantity_delivered: int | None
    notes: str | None


class OrderChargeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_type_id: UUID
    service_option_id: UUID | None
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class OrderDiscountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    promotion_id: UUID | None
    description: str
    amount: Decimal


class OrderPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount: Decimal
    method: PaymentMethod
    is_advance: bool
    reference: str | None
    received_by_id: UUID
    paid_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_date: date
    daily_number: int
    booklet_serial: str | None
    customer_id: UUID
    nit: str | None
    weight_lbs: Decimal | None
    total_pieces: int
    observations: str | None
    status: OrderStatus
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal
    #: Derived, not columns: total minus payments (§5.3). Sent anyway because the
    #: screen that shows a ticket always shows what is still owed, and making
    #: every client re-add the payments is asking for four different answers.
    paid_total: Decimal
    balance: Decimal
    received_by_id: UUID
    delivered_at: datetime | None = None
    delivered_by_id: UUID | None = None
    cancelled_at: datetime | None = None
    cancelled_by_id: UUID | None = None
    cancel_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    garments: list[OrderGarmentRead] = Field(default_factory=list)
    charges: list[OrderChargeRead] = Field(default_factory=list)
    discounts: list[OrderDiscountRead] = Field(default_factory=list)
    payments: list[OrderPaymentRead] = Field(default_factory=list)
    #: What the engine flagged but did not block (§6.3). Surfaced so the counter
    #: sees it, and deliberately not stored: a warning is about the moment of
    #: capture, and the ticket that carries it is already saved.
    warnings: list[str] = Field(default_factory=list)


class OrderSummary(BaseModel):
    """A row of the list. No lines: the list shows money and who, not detail."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_date: date
    daily_number: int
    booklet_serial: str | None
    customer_id: UUID
    total_pieces: int
    status: OrderStatus
    total: Decimal
    #: The day's list is where anyone asks "who still owes", so the row carries it.
    balance: Decimal
    created_at: datetime


class OrderPage(BaseModel):
    items: list[OrderSummary]
    total: int
    page: int
    page_size: int


class DailySummary(BaseModel):
    """The day's tickets added up (§8) — the seed of the daily close.

    Everything here is scoped to the orders **of that business date**, payments
    included. That is not the same question as "how much cash came in today":
    money paid today against last week's ticket belongs to today's till and to
    this date's `balance`, and reconciling the two is the daily close of the
    Plan 0005, not this endpoint.
    """

    order_date: date
    #: Every ticket of the day, voided ones included.
    orders: int
    by_status: dict[OrderStatus, int]
    #: Pieces on the tickets that still count — a voided one took nothing in.
    pieces: int
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal
    #: Money received against the day's tickets, **voided ones included**: it was
    #: handed over and it is in the drawer, and refunding it is a cash movement
    #: of its own (§7.3).
    collected: Decimal
    #: What the day's live tickets still owe.
    balance: Decimal
