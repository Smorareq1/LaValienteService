"""Which lots cover a sale, and what it costs (Plan 0005 D5).

Pure like `orders/pricing.py`: no session, no ORM rows, no clock. What a customer
is charged for five bags of detergent is a calculation that has to be readable on
its own and testable without a database — the persistence around it is the
service's problem.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from src.core.exceptions import ConflictError
from src.core.money import money

ZERO = Decimal("0.00")


class InsufficientStock(ConflictError):
    """The shelf cannot cover what was asked for.

    Carries the numbers and not just a sentence because two callers need them:
    the counter, which has to be told how many are actually left, and the sync
    applicator of PR 11, which attaches the server's current stock to the
    rejected operation so the review queue can show both sides (D12).

    A `ConflictError` so that an ordinary HTTP request already answers 409
    without anyone having to remember to translate it.
    """

    def __init__(self, product_name: str, requested: Decimal, available: Decimal) -> None:
        self.product_name = product_name
        self.requested = requested
        self.available = available
        super().__init__(
            f"There is not enough {product_name}: {requested} asked for, {available} available."
        )


@dataclass(frozen=True)
class LotStock:
    """What the allocation needs to know about one lot.

    A flat copy rather than the ORM row: the arithmetic must not be able to
    change stock as a side effect of reading it.
    """

    lot_id: UUID
    lot_number: int
    quantity_available: Decimal
    #: `None` marks a lot the laundry keeps for its own use. It is skipped rather
    #: than sold at zero (§5.2).
    sale_price: Decimal | None

    @property
    def is_sellable(self) -> bool:
        return self.sale_price is not None and self.quantity_available > ZERO


@dataclass(frozen=True)
class Allocation:
    """One lot's share of a sale line, priced at that lot's own price."""

    lot_id: UUID
    lot_number: int
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


def available_stock(lots: Iterable[LotStock]) -> Decimal:
    """What could actually be sold. Lots kept for internal use do not count."""
    return sum((lot.quantity_available for lot in lots if lot.is_sellable), ZERO)


def allocate_fifo(
    lots: Sequence[LotStock], quantity: Decimal, *, product_name: str
) -> list[Allocation]:
    """Cover `quantity` from the oldest lots first (D5).

    `lots` must already be in arrival order — that ordering is a database
    concern and belongs to the repository. What is decided here is that the
    oldest bottle leaves first, that a lot with no sale price is skipped rather
    than sold, and that the price of each share is the price of *its* lot: a sale
    that spans two lots bought at different prices is two lines, not an average.

    Raises `InsufficientStock` before returning anything, so a sale is never
    half-allocated.
    """
    if quantity <= ZERO:
        raise ConflictError("A sale line has to be for more than zero.")

    sellable = [lot for lot in lots if lot.is_sellable]
    on_hand = available_stock(sellable)
    if on_hand < quantity:
        raise InsufficientStock(product_name, quantity, on_hand)

    allocations: list[Allocation] = []
    remaining = quantity
    for lot in sellable:
        if remaining <= ZERO:
            break
        assert lot.sale_price is not None  # `is_sellable` already established it
        taken = min(remaining, lot.quantity_available)
        allocations.append(
            Allocation(
                lot_id=lot.lot_id,
                lot_number=lot.lot_number,
                quantity=taken,
                unit_price=lot.sale_price,
                amount=money(taken * lot.sale_price),
            )
        )
        remaining -= taken

    return allocations


def sale_total(allocations: Iterable[Allocation]) -> Decimal:
    """Σ of the lines, rounded the same way each of them was.

    Summing the already-rounded amounts and not the exact products: the printed
    lines have to add up to the printed total (see `core.money`).
    """
    return money(sum((allocation.amount for allocation in allocations), ZERO))
