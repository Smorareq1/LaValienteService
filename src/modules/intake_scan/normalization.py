"""Turning a reading into a draft (Plan 0003 §7).

Pure logic: no session, no HTTP, no provider. The caller hands over what the
model said plus the catalog of the day, and gets back the draft the app will
prefill — every id resolved, every amount recomputed, every disagreement turned
into a coded warning.

This is where the module earns its keep. The model reads handwriting; **this**
decides what is believable. A phone that is not eight digits, a garment name
nothing matches, a level outside its piece range, a total that does not add up —
each has a deterministic answer here, and none of them depends on the weather at
the provider.

Warnings travel as **codes** (`total_mismatch:108.75:98.75`), the way the sync
engine and the daily close already do it. The server is the only one that can
detect them; the sentence in Spanish belongs to the screen.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from src.modules.catalog.models import GarmentType
from src.modules.intake_scan.schemas import (
    CONFIDENT_AT_LEAST,
    DraftCharge,
    DraftField,
    DraftGarment,
    RawScan,
)
from src.modules.orders.pricing import ChargeRequest

#: How far from today a scanned date may sit before it is flagged. A booklet
#: caught up on Monday morning is normal; one dated next year is a misread year.
DATE_TOLERANCE = timedelta(days=30)

#: Above this gap between the total on the paper and the recomputed one, the
#: counter is told. Q1 absorbs the rounding of a handwritten sum.
TOTAL_TOLERANCE = Decimal("1.00")

#: The boxes of the ticket, mapped to the service codes the catalog seeds.
#: The paper says "R"; the catalog says `extra_softener`. This table is the
#: whole of that translation, and it is the first thing to check when a new
#: service is added to the printed form.
EXTRA_SERVICE_CODES = {
    "rins": "extra_softener",
    "spin": "extra_spin",
    "t10_lapses": "extra_dry_time",
    "urgent": "urgent_service",
}

WASH_BY_WEIGHT = "wash_by_weight"
TIERED_BOXES = {"wash_tub": "wash_tub", "dry": "dry", "hand_wash": "hand_wash"}
PICKUP_CODE = "pickup"
DELIVERY_CODE = "delivery"

#: Written on the ticket, meaning "no NIT". The customer module accepts it.
FINAL_CONSUMER = "CF"


@dataclass
class Normalized:
    """What §7 produces before the pricing engine is asked anything."""

    order_date: DraftField[date]
    daily_number: DraftField[int]
    booklet_serial: DraftField[str]
    nit: DraftField[str]
    weight_lbs: DraftField[Decimal]
    observations: DraftField[str]
    customer_name: DraftField[str]
    customer_phone: DraftField[str]
    customer_address: DraftField[str]
    garments: list[DraftGarment]
    charges: list[ChargeRequest]
    #: Parallel to `charges`: the confidence each line was read with.
    charge_confidence: list[float]
    total_read: Decimal | None
    warnings: list[str] = field(default_factory=list)


def strip_accents(value: str) -> str:
    """`Pantalón` and `pantalon` are the same garment to anyone writing quickly."""
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def normalize_name(value: str) -> str:
    """The key garment names are matched on: no accents, no case, no padding."""
    return " ".join(strip_accents(value).lower().split())


def normalize_phone(value: str | None) -> str | None:
    """Eight digits, or nothing (§7.1).

    Guatemalan numbers are eight digits, written `5512-3456` or `5512 3456`. A
    country code is dropped rather than kept: the customers module stores the
    local number, and a `502` prefix would make the same person look like two.
    Anything else comes back `None` — a half-read phone is worse than a blank
    one, because it will be dialled.
    """
    if not value:
        return None
    digits = "".join(char for char in value if char.isdigit())
    if digits.startswith("502") and len(digits) == 11:
        digits = digits[3:]
    return digits if len(digits) == 8 else None


def normalize_nit(value: str | None) -> str | None:
    """`CF`, or digits with an optional check character (§7.1)."""
    if not value:
        return None
    cleaned = "".join(value.split()).upper()
    if cleaned in {"CF", "C/F", "C.F."}:
        return FINAL_CONSUMER
    body = cleaned.replace("-", "")
    if not body:
        return None
    digits, check = body[:-1], body[-1]
    if check.isdigit():
        digits, check = body, ""
    if not digits.isdigit() or not 5 <= len(digits) <= 12:
        return None
    if check and check != "K":
        return None
    return f"{digits}{'-' + check if check else ''}"


def normalize_serial(value: str | None) -> str | None:
    """The printed booklet serial, as the one string it actually is.

    Three things and no more: the surrounding whitespace, the `#` the paper
    prints in front of it (`#Tomapedido 0042`), and the case. Hyphens and the
    rest are left alone — what the print shop puts on the sheet is the
    identifier, and this is not the place to decide which of its characters
    count.

    It matters twice over. On the way in, it is what stops `A-0042` and
    `#a-0042` from being two tickets as far as the duplicate index is concerned.
    On the way out, a delivery lookup compares this against what is stored, and
    a comparison is only as good as both sides being spelled the same way.
    """
    if value is None:
        return None
    cleaned = " ".join(value.split()).lstrip("#").strip().upper()
    return cleaned or None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def draft_field[T](value: T | None, confidence: float, raw: str | None = None) -> DraftField[T]:
    """A draft field, marked for review when the reading was not confident."""
    return DraftField[T](
        value=value,
        confidence=confidence,
        raw_text=raw,
        needs_review=value is not None and confidence < CONFIDENT_AT_LEAST,
    )


def resolve_date(raw: RawScan, today: date) -> tuple[DraftField[date], list[str]]:
    """The date box, or today (§7.1).

    The talonario prints Xs where nothing was written, so a blank date is the
    normal case and not an error: it means the ticket is of the day it was taken,
    which is exactly what `OrderCreate` means by omitting `order_date`.
    """
    box = raw.header.date
    parts = (box.day.value, box.month.value, box.year.value)
    confidence = min(box.day.confidence, box.month.confidence, box.year.confidence)

    if any(part is None for part in parts):
        return draft_field(today, 1.0, None), []

    day, month, year = parts
    assert day is not None and month is not None and year is not None
    try:
        parsed = date(year, month, day)
    except ValueError:
        # A 31st of February is a misread digit, not a date. Falling back to
        # today keeps the capture moving and the warning says why.
        return draft_field(today, 1.0), [f"date_unreadable:{year}-{month}-{day}"]

    warnings: list[str] = []
    if abs(parsed - today) > DATE_TOLERANCE:
        warnings.append(f"date_far_from_today:{parsed.isoformat()}")
    return draft_field(parsed, confidence), warnings


def resolve_garments(
    raw: RawScan, garment_types: list[GarmentType]
) -> tuple[list[DraftGarment], list[str]]:
    """Map what was read onto the 21 kinds printed on the ticket (§7.2).

    Unmatched lines are **dropped with a warning** rather than guessed at. A
    garment the catalog does not know cannot be saved, and inventing the nearest
    one would put a piece of clothing on a ticket that nobody brought in.
    """
    by_key = {normalize_name(kind.name): kind for kind in garment_types if kind.is_active}
    # The printed names are compound (`Pants/Pijama`, `Playera/Polo`), and people
    # write one half of them. Each half points at the whole.
    for kind in garment_types:
        if not kind.is_active:
            continue
        for half in kind.name.split("/"):
            by_key.setdefault(normalize_name(half), kind)

    resolved: list[DraftGarment] = []
    warnings: list[str] = []

    for line in raw.garments:
        name, quantity = line.name.value, line.quantity.value
        if not name or not quantity or quantity <= 0:
            continue
        matched = by_key.get(normalize_name(name))
        if matched is None:
            warnings.append(f"garment_unmatched:{name}")
            continue
        confidence = min(line.name.confidence, line.quantity.confidence)
        resolved.append(
            DraftGarment(
                garment_type_id=matched.id,
                name=matched.name,
                quantity=quantity,
                confidence=confidence,
                needs_review=confidence < CONFIDENT_AT_LEAST,
            )
        )

    return resolved, warnings


def resolve_charges(raw: RawScan) -> tuple[list[ChargeRequest], list[float], list[str]]:
    """Turn the service boxes into charge requests (§7.3).

    No prices anywhere (D4): what comes out is what, which option, and how many.
    The two exceptions are pickup and delivery, whose fee the courier writes on
    the paper because no catalog holds it.
    """
    charges: list[ChargeRequest] = []
    confidences: list[float] = []
    warnings: list[str] = []
    services = raw.services

    pounds = _decimal(services.wash_by_weight_lbs.value)
    if pounds is not None and pounds > 0:
        charges.append(ChargeRequest(service_code=WASH_BY_WEIGHT, quantity=pounds))
        confidences.append(services.wash_by_weight_lbs.confidence)

    for box, service_code in TIERED_BOXES.items():
        counts: dict[str, Any] = getattr(services, box)
        for option_code, read in counts.items():
            quantity = read.value
            if not quantity or quantity <= 0:
                continue
            charges.append(
                ChargeRequest(
                    service_code=service_code,
                    option_code=option_code,
                    quantity=Decimal(quantity),
                )
            )
            confidences.append(read.confidence)

    for box_code, service_code in EXTRA_SERVICE_CODES.items():
        read = services.extras.get(box_code)
        if read is None or not read.value or read.value <= 0:
            continue
        charges.append(
            ChargeRequest(service_code=service_code, quantity=Decimal(read.value))
        )
        confidences.append(read.confidence)

    for read, code in (
        (services.pickup_amount, PICKUP_CODE),
        (services.delivery_amount, DELIVERY_CODE),
    ):
        amount = _decimal(read.value)
        if amount is None or amount <= 0:
            continue
        charges.append(ChargeRequest(service_code=code, amount=amount))
        confidences.append(read.confidence)

    if not charges:
        # Not an error: an unreadable services block still leaves a customer, a
        # date and the garments, and the counter finishes by hand. Saying so is
        # what stops it looking like a free ticket.
        warnings.append("no_services_read")

    return charges, confidences, warnings


def check_weight(
    weight: Decimal | None, charges: list[ChargeRequest]
) -> tuple[Decimal | None, list[str]]:
    """§7.3: the header weight and the pounds being billed are one number.

    The engine of Plan 0001 §6 *refuses* a ticket where they disagree. Here that
    would be a dead end — the person cannot argue with a photograph — so the
    header is made to follow the billed pounds and the disagreement is reported.
    """
    billed = sum(
        (line.quantity for line in charges if line.service_code == WASH_BY_WEIGHT),
        Decimal(0),
    )
    if billed <= 0:
        return weight, []
    if weight is None:
        return billed, ["weight_taken_from_charge"]
    if weight != billed:
        return billed, [f"weight_mismatch:{weight}:{billed}"]
    return weight, []


def check_total(read: Decimal | None, computed: Decimal) -> list[str]:
    """§7.4: the total on the paper against the one the catalog works out.

    A gap here almost always means a quantity was read wrong, which is why it is
    worth showing: it points at the line to look at, not at the arithmetic.
    """
    if read is None or read <= 0:
        return []
    if abs(read - computed) > TOTAL_TOLERANCE:
        return [f"total_mismatch:{read}:{computed}"]
    return []


def normalize(raw: RawScan, *, today: date, garment_types: list[GarmentType]) -> Normalized:
    """Everything of §7 that needs no database beyond the garment catalog."""
    warnings: list[str] = []

    order_date, date_warnings = resolve_date(raw, today)
    warnings.extend(date_warnings)

    garments, garment_warnings = resolve_garments(raw, garment_types)
    warnings.extend(garment_warnings)

    charges, confidences, charge_warnings = resolve_charges(raw)
    warnings.extend(charge_warnings)

    weight, weight_warnings = check_weight(
        _decimal(raw.header.weight_lbs.value), charges
    )
    warnings.extend(weight_warnings)

    phone = normalize_phone(raw.customer.phone.value)
    if raw.customer.phone.value and phone is None:
        warnings.append(f"phone_unreadable:{raw.customer.phone.value}")

    nit = normalize_nit(raw.header.nit.value)
    if raw.header.nit.value and nit is None:
        warnings.append(f"nit_unreadable:{raw.header.nit.value}")

    return Normalized(
        order_date=order_date,
        daily_number=draft_field(
            raw.header.daily_number.value,
            raw.header.daily_number.confidence,
            raw.header.daily_number.raw_text,
        ),
        booklet_serial=draft_field(
            normalize_serial(raw.header.booklet_serial.value),
            raw.header.booklet_serial.confidence,
            raw.header.booklet_serial.raw_text,
        ),
        nit=draft_field(nit, raw.header.nit.confidence, raw.header.nit.raw_text),
        weight_lbs=draft_field(weight, raw.header.weight_lbs.confidence),
        observations=draft_field(
            raw.observations.value, raw.observations.confidence, raw.observations.raw_text
        ),
        customer_name=draft_field(
            raw.customer.full_name.value,
            raw.customer.full_name.confidence,
            raw.customer.full_name.raw_text,
        ),
        customer_phone=draft_field(
            phone, raw.customer.phone.confidence, raw.customer.phone.raw_text
        ),
        customer_address=draft_field(
            raw.customer.address.value, raw.customer.address.confidence
        ),
        garments=garments,
        charges=charges,
        charge_confidence=confidences,
        total_read=_decimal(raw.totals_read.total.value),
        warnings=warnings,
    )


def draft_charges(
    charges: list[ChargeRequest],
    confidences: list[float],
    priced_by_index: dict[int, tuple[str, Decimal]],
) -> list[DraftCharge]:
    """Pair each request with what the engine made of it, for the form."""
    lines: list[DraftCharge] = []
    for index, (request, confidence) in enumerate(zip(charges, confidences, strict=True)):
        description, amount = priced_by_index.get(index, ("", Decimal(0)))
        lines.append(
            DraftCharge(
                service_code=request.service_code,
                option_code=request.option_code,
                quantity=request.quantity,
                amount=request.amount,
                description=description,
                estimated_amount=amount,
                confidence=confidence,
                needs_review=confidence < CONFIDENT_AT_LEAST,
            )
        )
    return lines
