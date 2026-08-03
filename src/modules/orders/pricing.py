"""What a ticket costs (Plan 0001 §6).

Pure logic: no session, no FastAPI, no I/O. The caller hands over the catalog as
of the order's date and gets back the lines with their amounts already frozen.
That is what makes the rule that decides how much a customer pays testable
without a database, and it is why the plan puts D5 the way it does — the client
sends quantities and choices, never money.

The plan places this in `service.py`; it lives in its own module so the arithmetic
can be read, and tested, without the persistence around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from src.core.exceptions import ConflictError
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.catalog.service import resolve_price

#: Weight-based washing: the one service whose quantity is also a header field.
WASH_BY_WEIGHT_CODE = "wash_by_weight"
#: Hand washing, priced by a piece-count level (N2/N3/N4).
HAND_WASH_CODE = "hand_wash"

CENTS = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Round to cents, half away from zero — how a person rounds on paper.

    Applied per line and then summed, rather than summing exact values and
    rounding at the end, so the printed lines add up to the printed subtotal. A
    customer checking the ticket by hand must not find it off by a cent.
    """
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ChargeRequest:
    """A line as the counter captured it: what, how many, and nothing else."""

    service_code: str
    option_code: str | None = None
    quantity: Decimal = Decimal(1)
    #: Only for `variable` services, where the courier's fee is typed in.
    amount: Decimal | None = None


@dataclass(frozen=True)
class DiscountRequest:
    """Money off. In PR 3 always manual; promotions join in PR 5."""

    description: str
    amount: Decimal


@dataclass(frozen=True)
class PricedCharge:
    """A resolved line, ready to be written down as a snapshot (D2)."""

    service_type_id: UUID
    service_option_id: UUID | None
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class PricedDiscount:
    promotion_id: UUID | None
    description: str
    amount: Decimal


@dataclass(frozen=True)
class PricedOrder:
    charges: list[PricedCharge]
    discounts: list[PricedDiscount]
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal
    #: Things worth saying out loud that must not stop the counter. The paper
    #: ticket handled these with human judgement and so does this.
    warnings: list[str] = field(default_factory=list)


class PriceBook:
    """The catalog frozen at one date, indexed the way the engine reads it."""

    def __init__(
        self, service_types: list[ServiceType], prices: list[ServicePrice], on_date: date
    ) -> None:
        self.on_date = on_date
        self._services = {service.code: service for service in service_types}
        self._prices = prices

    def service(self, code: str) -> ServiceType:
        service = self._services.get(code)
        if service is None:
            raise ConflictError(f"There is no active service with code '{code}'.")
        return service

    @staticmethod
    def option(service: ServiceType, code: str) -> ServiceOption:
        for option in service.options:
            if option.code == code and option.deleted_at is None and option.is_active:
                return option
        raise ConflictError(f"Service '{service.code}' has no active option '{code}'.")

    def price(self, service: ServiceType, option: ServiceOption | None) -> Decimal:
        resolved = resolve_price(
            self._prices,
            (service.id, option.id if option is not None else None),
            self.on_date,
        )
        if resolved is None:
            what = f"{service.code}/{option.code}" if option is not None else service.code
            # Never fall back to zero: a ticket that quietly costs nothing is far
            # worse than one that refuses to be taken.
            raise ConflictError(
                f"'{what}' has no price in force on {self.on_date.isoformat()}."
            )
        return resolved


def price_charge(request: ChargeRequest, book: PriceBook) -> PricedCharge:
    """Resolve one line against the catalog in force (§6.2 step 1)."""
    service = book.service(request.service_code)

    if service.pricing_mode is PricingMode.VARIABLE:
        if request.amount is None:
            raise ConflictError(
                f"'{service.code}' is charged at whatever it cost, so it needs an amount."
            )
        if request.amount < 0:
            raise ConflictError(f"The amount for '{service.code}' cannot be negative.")
        if request.option_code is not None:
            raise ConflictError(f"'{service.code}' has no options to choose from.")
        amount = money(request.amount)
        # Quantity stays 1: the line *is* the fee, and the DB check demands > 0.
        return PricedCharge(
            service_type_id=service.id,
            service_option_id=None,
            description=service.name,
            quantity=Decimal(1),
            unit_price=amount,
            amount=amount,
        )

    if request.amount is not None:
        raise ConflictError(
            f"'{service.code}' takes its price from the catalog, so it accepts no amount."
        )
    if request.quantity <= 0:
        raise ConflictError(f"The quantity for '{service.code}' has to be greater than zero.")

    option: ServiceOption | None = None
    if service.pricing_mode is PricingMode.TIERED:
        if request.option_code is None:
            raise ConflictError(f"'{service.code}' is charged by option, so one must be chosen.")
        option = book.option(service, request.option_code)
    elif request.option_code is not None:
        raise ConflictError(f"'{service.code}' has a single price, so it takes no option.")

    unit_price = book.price(service, option)
    description = service.name if option is None else f"{service.name} — {option.name}"
    return PricedCharge(
        service_type_id=service.id,
        service_option_id=option.id if option is not None else None,
        description=description[:160],
        quantity=request.quantity,
        unit_price=unit_price,
        amount=money(request.quantity * unit_price),
    )


def price_order(
    charges: list[ChargeRequest],
    discounts: list[DiscountRequest],
    book: PriceBook,
    *,
    weight_lbs: Decimal | None = None,
    total_pieces: int = 0,
) -> PricedOrder:
    """Turn what was captured into what is owed (§6.2).

    Raises :class:`ConflictError` on anything that would make the total wrong;
    returns warnings for what only a person can judge.
    """
    if not charges:
        raise ConflictError("An order needs at least one charge.")

    priced = [price_charge(request, book) for request in charges]
    _check_weight(charges, weight_lbs)
    warnings = _hand_wash_warnings(charges, book, total_pieces)

    subtotal = money(sum((line.amount for line in priced), Decimal(0)))

    priced_discounts = [
        PricedDiscount(promotion_id=None, description=item.description, amount=money(item.amount))
        for item in discounts
    ]
    for discount in priced_discounts:
        if discount.amount <= 0:
            raise ConflictError("A discount has to be greater than zero.")

    discount_total = money(sum((item.amount for item in priced_discounts), Decimal(0)))
    if discount_total > subtotal:
        # §6.2 step 4. A ticket that owes the customer money is not a discount,
        # it is a typo.
        raise ConflictError("The discount cannot exceed the subtotal.")

    return PricedOrder(
        charges=priced,
        discounts=priced_discounts,
        subtotal=subtotal,
        discount_total=discount_total,
        total=money(subtotal - discount_total),
        warnings=warnings,
    )


def _check_weight(charges: list[ChargeRequest], weight_lbs: Decimal | None) -> None:
    """§6.3: the weight on the header and the weight being billed are one number.

    They are captured twice — once as a header field the app shows large, once as
    the quantity of the by-weight line — and letting them drift would leave a
    ticket that says 12 lb and charges 21.
    """
    by_weight = [line for line in charges if line.service_code == WASH_BY_WEIGHT_CODE]
    if not by_weight:
        return
    if weight_lbs is None:
        raise ConflictError("Washing by weight needs the weight in pounds.")
    billed = sum((line.quantity for line in by_weight), Decimal(0))
    if billed != weight_lbs:
        raise ConflictError(
            f"The weight on the ticket ({weight_lbs}) does not match "
            f"the pounds being charged ({billed})."
        )


def _hand_wash_warnings(
    charges: list[ChargeRequest], book: PriceBook, total_pieces: int
) -> list[str]:
    """§6.3: a level outside its piece range is flagged, never blocked.

    The counter picks the level while the customer waits; the paper ticket
    settled these with judgement, and refusing the sale over it would be worse
    than saying so.
    """
    warnings: list[str] = []
    for line in charges:
        if line.service_code != HAND_WASH_CODE or line.option_code is None:
            continue
        service = book.service(line.service_code)
        option = book.option(service, line.option_code)
        low, high = option.min_quantity, option.max_quantity
        if low is None and high is None:
            continue
        if (low is not None and total_pieces < low) or (high is not None and total_pieces > high):
            span = f"{low or 0} to {high}" if high is not None else f"{low} or more"
            warnings.append(
                f"Level {option.code} covers {span} pieces and the order declares {total_pieces}."
            )
    return warnings
