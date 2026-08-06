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
from decimal import Decimal
from uuid import UUID

from src.core.exceptions import ConflictError
from src.core.money import money
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.catalog.service import resolve_price
from src.modules.promotions.models import Promotion

#: Weight-based washing: the one service whose quantity is also a header field.
WASH_BY_WEIGHT_CODE = "wash_by_weight"
#: Hand washing, priced by a piece-count level (N2/N3/N4).
HAND_WASH_CODE = "hand_wash"



@dataclass(frozen=True)
class ChargeRequest:
    """A line as the counter captured it: what, how many, and nothing else."""

    service_code: str
    option_code: str | None = None
    quantity: Decimal = Decimal(1)
    #: Only for `variable` services, where the courier's fee is typed in.
    amount: Decimal | None = None


@dataclass(frozen=True)
class ManualDiscount:
    """Money off with nothing but a person's judgement behind it (D10).

    Demands `orders.manual_discount`, which only an administrator holds — the
    amount is decided at the counter and nothing checks it afterwards.
    """

    description: str
    amount: Decimal


@dataclass(frozen=True)
class PromotionDiscount:
    """A promotion the counter picked. The engine works out how much it is.

    Only the code travels, never an amount: a device that could name its own
    figure would make D5 ("the client sends quantities, never money") true of
    charges and false of discounts.
    """

    promotion_code: str


#: What may appear on the discounts list of a capture.
DiscountRequest = ManualDiscount | PromotionDiscount


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


class PromotionBook:
    """The promotions in force at one date, indexed by the code the app sends."""

    def __init__(self, promotions: list[Promotion], on_date: date) -> None:
        self.on_date = on_date
        self._promotions = {promotion.code: promotion for promotion in promotions}

    def resolve(self, code: str) -> Promotion:
        promotion = self._promotions.get(code)
        if promotion is None:
            raise ConflictError(f"There is no promotion with code '{code}'.")
        if not promotion.covers(self.on_date):
            # Plan 0004 §8 files this under "promoción vencida al momento de
            # aplicar": an order captured offline while the promotion was live
            # can reach the server after it ended, and the person has to be told
            # what the ticket costs without it rather than have it applied anyway.
            raise ConflictError(
                f"The promotion '{promotion.name}' is not in force "
                f"on {self.on_date.isoformat()}."
            )
        return promotion


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
    promotions: PromotionBook | None = None,
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

    priced_discounts = _price_discounts(discounts, charges, priced, promotions)

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


def _price_discounts(
    discounts: list[DiscountRequest],
    charges: list[ChargeRequest],
    priced: list[PricedCharge],
    promotions: PromotionBook | None,
) -> list[PricedDiscount]:
    """Resolve every discount to an amount (§6.2 step 3).

    Promotions and manual amounts share one list so the ticket keeps them in the
    order they were captured, which is the order the counter will read back.
    """
    resolved: list[PricedDiscount] = []
    applied_codes: set[str] = set()

    for item in discounts:
        if isinstance(item, ManualDiscount):
            amount = money(item.amount)
            if amount <= 0:
                raise ConflictError("A discount has to be greater than zero.")
            resolved.append(
                PricedDiscount(
                    promotion_id=None, description=item.description, amount=amount
                )
            )
            continue

        if promotions is None:
            raise ConflictError("This order was priced without the promotions of the day.")
        if item.promotion_code in applied_codes:
            # Twice is never intended: the second tap is a slip, and letting it
            # through would take the discount off two times.
            raise ConflictError(f"The promotion '{item.promotion_code}' is already applied.")
        applied_codes.add(item.promotion_code)

        promotion = promotions.resolve(item.promotion_code)
        base = _applicable_base(promotion, charges, priced)
        amount = money(promotion.discount_on(base))
        if amount <= 0:
            # Dropping it in silence would leave the counter certain a discount
            # was applied, and the customer paying the full price anyway.
            raise ConflictError(
                f"The promotion '{promotion.name}' takes nothing off this order."
            )
        resolved.append(
            PricedDiscount(
                promotion_id=promotion.id,
                # A snapshot, like a charge's description (D2): the promotion may
                # be renamed or retired, and the ticket has to keep reading the
                # way it did the day it was taken.
                description=promotion.name[:160],
                amount=amount,
            )
        )

    return resolved


def _applicable_base(
    promotion: Promotion, charges: list[ChargeRequest], priced: list[PricedCharge]
) -> Decimal:
    """The lines the promotion bites on, added up (§5.4).

    Read off the requests rather than the priced lines because the service code
    is what the promotion names, and only the request carries it — the two lists
    are the same lines in the same order.
    """
    return money(
        sum(
            (
                line.amount
                for request, line in zip(charges, priced, strict=True)
                if promotion.applies_to(request.service_code)
            ),
            Decimal(0),
        )
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
