"""The extraction contract (Plan 0003 §6) and what the app gets back.

Two families of shapes live here and they are not the same thing:

* `Field*` / `RawScan…` — what **Gemini** returns, where every leaf carries its
  own `confidence` and the literal `raw_text` it read (D3). Nothing here is
  trusted: it is a reading of somebody's handwriting.
* `ScanDraft…` — what **we** hand the app after validating, normalizing and
  repricing (§7). Ids resolved, amounts recomputed by the engine of Plan 0001
  §6, discrepancies turned into coded warnings.

Keeping them apart is what stops a `confidence` from leaking into the order
capture, and what makes the second family safe to prefill a form with.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.modules.intake_scan.models import ScanStatus
from src.modules.orders.models import OrderStatus

# --------------------------------------------------------------------- lectura

#: Below this a field is not prefilled at all: it is shown empty and marked, and
#: the form puts the initial focus on the first of them (§4).
ILLEGIBLE_BELOW = 0.50
#: Between this and `ILLEGIBLE_BELOW` the value is prefilled but flagged "check".
CONFIDENT_AT_LEAST = 0.85


class FieldRead[T](BaseModel):
    """One leaf of the extraction: what it says, how sure, and what it looked like.

    `raw_text` is not decoration. When the counter disagrees with `value`, the
    literal reading is what tells apart a bad transcription from bad handwriting,
    and it is what the golden set of §9 is scored against.
    """

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_text: str | None = None

    @property
    def is_legible(self) -> bool:
        return self.value is not None and self.confidence >= ILLEGIBLE_BELOW

    @property
    def needs_review(self) -> bool:
        return self.value is not None and self.confidence < CONFIDENT_AT_LEAST


class RawScanDate(BaseModel):
    """The date box. The talonario prints Xs where nothing was written."""

    day: FieldRead[int] = Field(default_factory=FieldRead[int])
    month: FieldRead[int] = Field(default_factory=FieldRead[int])
    year: FieldRead[int] = Field(default_factory=FieldRead[int])


class RawScanHeader(BaseModel):
    date: RawScanDate = Field(default_factory=RawScanDate)
    #: The "No." of the booklet — the number everyone calls the ticket by.
    daily_number: FieldRead[int] = Field(default_factory=FieldRead[int])
    #: The printed "#Tomapedido" serial, which is what detects a duplicate.
    booklet_serial: FieldRead[str] = Field(default_factory=FieldRead[str])
    weight_lbs: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    #: Printed on the paper as "Factura".
    nit: FieldRead[str] = Field(default_factory=FieldRead[str])


class RawScanCustomer(BaseModel):
    full_name: FieldRead[str] = Field(default_factory=FieldRead[str])
    phone: FieldRead[str] = Field(default_factory=FieldRead[str])
    address: FieldRead[str] = Field(default_factory=FieldRead[str])
    email: FieldRead[str] = Field(default_factory=FieldRead[str])


class RawScanGarment(BaseModel):
    name: FieldRead[str] = Field(default_factory=FieldRead[str])
    quantity: FieldRead[int] = Field(default_factory=FieldRead[int])


class RawScanServices(BaseModel):
    """The service boxes of the ticket, as counts per option.

    Deliberately **no prices** (D4). What is read off the paper is "2 tinas G",
    "T50", "12.5 lbs"; the money comes from the catalog in force. A misread price
    can therefore never reach a ticket.
    """

    wash_by_weight_lbs: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    #: `{"G": 2}` — option code to how many.
    wash_tub: dict[str, FieldRead[int]] = Field(default_factory=dict)
    dry: dict[str, FieldRead[int]] = Field(default_factory=dict)
    hand_wash: dict[str, FieldRead[int]] = Field(default_factory=dict)
    extras: dict[str, FieldRead[int]] = Field(default_factory=dict)
    pickup_amount: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    delivery_amount: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])


class RawScanTotals(BaseModel):
    """What the paper says it cost. **Cross-check only** (D4).

    These numbers never become a charge. They exist so that a total that differs
    from the recomputed one can raise the warning of §7.4, which is usually a
    quantity that was read wrong.
    """

    subtotal: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    discount: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    total: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])


class RawScan(BaseModel):
    """The whole of what the model returns, before we believe any of it."""

    model_config = ConfigDict(extra="ignore")

    header: RawScanHeader = Field(default_factory=RawScanHeader)
    customer: RawScanCustomer = Field(default_factory=RawScanCustomer)
    garments: list[RawScanGarment] = Field(default_factory=list)
    observations: FieldRead[str] = Field(default_factory=FieldRead[str])
    services: RawScanServices = Field(default_factory=RawScanServices)
    discounts_marked: list[FieldRead[str]] = Field(default_factory=list)
    totals_read: RawScanTotals = Field(default_factory=RawScanTotals)


# --------------------------------------------------------------------- borrador


class DraftField[T](BaseModel):
    """A value for the form, with the badge the screen paints on it (§4)."""

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_text: str | None = None
    #: `true` when the value is prefilled but should be looked at (0.50 to 0.85).
    needs_review: bool = False


class DraftGarment(BaseModel):
    """A garment line already resolved against `garment_types`."""

    garment_type_id: UUID
    name: str
    quantity: int
    confidence: float = 0.0
    needs_review: bool = False


class DraftCharge(BaseModel):
    """A line the way the capture screen of Plan 0002 sends it.

    Same shape as `OrderChargeCreate` on purpose: the draft is what the form will
    submit, so anything that would not be accepted by `POST /orders` has no
    business being prefilled.
    """

    service_code: str
    option_code: str | None = None
    quantity: Decimal = Decimal(1)
    amount: Decimal | None = None
    #: The description the catalog gives it, so the screen can name the line
    #: without resolving the code itself.
    description: str = ""
    #: What the engine works out it costs, for the live preview. Not authoritative
    #: — `POST /orders` recomputes it (D5).
    estimated_amount: Decimal | None = None
    confidence: float = 0.0
    needs_review: bool = False


class CustomerMatch(BaseModel):
    """A suggestion, never a decision (§7.5). Nothing is ever auto-created."""

    customer_id: UUID
    full_name: str
    phone: str | None = None
    #: 1.0 for an exact phone hit, lower for a fuzzy name.
    score: float = Field(ge=0.0, le=1.0)
    matched_on: str


class ScanDraft(BaseModel):
    """The prefill for the capture screen of Plan 0002."""

    order_date: DraftField[date] = Field(default_factory=DraftField[date])
    daily_number: DraftField[int] = Field(default_factory=DraftField[int])
    booklet_serial: DraftField[str] = Field(default_factory=DraftField[str])
    nit: DraftField[str] = Field(default_factory=DraftField[str])
    weight_lbs: DraftField[Decimal] = Field(default_factory=DraftField[Decimal])
    observations: DraftField[str] = Field(default_factory=DraftField[str])

    customer_name: DraftField[str] = Field(default_factory=DraftField[str])
    customer_phone: DraftField[str] = Field(default_factory=DraftField[str])
    customer_address: DraftField[str] = Field(default_factory=DraftField[str])
    customer_match: CustomerMatch | None = None

    garments: list[DraftGarment] = Field(default_factory=list)
    charges: list[DraftCharge] = Field(default_factory=list)

    #: Recomputed by the engine, for the footer. The official figure is still the
    #: one `POST /orders` returns (D5, and D10 of Plan 0004).
    estimated_subtotal: Decimal = Decimal(0)
    estimated_total: Decimal = Decimal(0)
    #: What the paper claimed, kept apart so the screen can show both sides of a
    #: mismatch without ever charging the read one.
    total_read: Decimal | None = None


class ScanRead(BaseModel):
    """`POST /scans` and `GET /scans/{id}`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ScanStatus
    model: str
    prompt_version: str
    latency_ms: int | None = None
    order_id: UUID | None = None
    error: str | None = None
    #: Coded, like the sync engine's and the daily close's: the server counts,
    #: the app writes the sentence in Spanish (Plan 0006, UI 8 note (c)).
    warnings: list[str] = Field(default_factory=list)
    draft: ScanDraft | None = None


# ---------------------------------------------------------------- búsqueda

#: What a match was found by, worst to best. The serial is printed and unique
#: across the booklet; the number is handwritten and only unique within its day.
MATCHED_ON_DAILY_NUMBER = "daily_number"
MATCHED_ON_BOOKLET_SERIAL = "booklet_serial"


class ScanLookupMatch(BaseModel):
    """A ticket the photograph could be pointing at.

    Deliberately not an `OrderSummary`: this is the answer to "which ticket is
    this piece of paper", so it carries what the counter needs to recognise it
    and settle it — the number, the money, the state — and `matched_on`, which
    is how sure the server is that it is the right one.

    The customer's name is **not** here. The device already mirrors customers
    (Plan 0004), so it can name the ticket itself, and sending it back would put
    a name on the wire for every photo taken at the counter.
    """

    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    order_date: date
    daily_number: int
    booklet_serial: str | None = None
    customer_id: UUID
    status: OrderStatus
    total_pieces: int
    total: Decimal
    paid_total: Decimal
    balance: Decimal
    matched_on: str


class ScanLookupRead(BaseModel):
    """`POST /scans/lookup` — the ticket in somebody's hand, identified.

    No `draft`. Nothing here is going to be captured: the ticket exists, and
    what comes back is *which* one plus what was read off the paper, so the
    screen can show the counter what the model thought it saw when the two
    disagree.

    `matches` is a list and usually holds one. Zero means the paper could not be
    read or names a ticket this database does not have; two means the printed
    serial and the handwritten number point at different tickets, and that is a
    question for the person holding it, not for the server.
    """

    id: UUID
    status: ScanStatus
    model: str
    prompt_version: str
    latency_ms: int | None = None
    error: str | None = None
    #: Coded, like everywhere else: `ticket_unreadable`, `no_match:4`,
    #: `serial_and_number_disagree`. The app writes the Spanish.
    warnings: list[str] = Field(default_factory=list)

    #: What was read off the paper, with its confidence — so a wrong match can
    #: be explained ("leyó 9 donde dice 4") instead of just being wrong.
    order_date: DraftField[date] = Field(default_factory=DraftField[date])
    daily_number: DraftField[int] = Field(default_factory=DraftField[int])
    booklet_serial: DraftField[str] = Field(default_factory=DraftField[str])

    matches: list[ScanLookupMatch] = Field(default_factory=list)


class ScanCorrections(BaseModel):
    """The diff between what was proposed and what was saved (D7)."""

    fields: dict[str, Any] = Field(default_factory=dict)
    corrected: int = 0
    proposed: int = 0

    @property
    def accuracy(self) -> float:
        if self.proposed == 0:
            return 1.0
        return 1.0 - (self.corrected / self.proposed)
