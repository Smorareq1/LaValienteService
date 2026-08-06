"""Which lots cover a sale, and what it comes to (Plan 0005 D5).

Pure arithmetic: no session, no products, no clock. What the service adds around
it — stock coming down, the kardex, voiding — is in `test_inventory_service`.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError
from src.modules.inventory.allocation import (
    InsufficientStock,
    LotStock,
    allocate_fifo,
    available_stock,
    sale_total,
)

OLDEST = uuid4()
NEWER = uuid4()
INTERNAL = uuid4()


def lot(lot_id: UUID, number: int, available: str, price: str | None = "10.00") -> LotStock:
    return LotStock(
        lot_id=lot_id,
        lot_number=number,
        quantity_available=Decimal(available),
        sale_price=None if price is None else Decimal(price),
    )


class TestAllocation:
    def test_one_lot_covers_the_line(self) -> None:
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "10.00")], Decimal("5.00"), product_name="Detergente"
        )

        assert len(allocations) == 1
        assert allocations[0].lot_number == 7
        assert allocations[0].quantity == Decimal("5.00")
        assert allocations[0].amount == Decimal("50.00")

    def test_the_oldest_lot_goes_first(self) -> None:
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "2.00", "10.00"), lot(NEWER, 8, "10.00", "12.00")],
            Decimal("3.00"),
            product_name="Detergente",
        )

        assert [allocation.lot_number for allocation in allocations] == [7, 8]
        assert allocations[0].quantity == Decimal("2.00")
        assert allocations[1].quantity == Decimal("1.00")

    def test_each_share_keeps_its_own_lot_price(self) -> None:
        """A sale spanning two purchases is two lines, not an average.

        The bottles cost the laundry different things and the customer is told
        which is which; averaging them would make the receipt unexplainable.
        """
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "2.00", "10.00"), lot(NEWER, 8, "10.00", "12.00")],
            Decimal("3.00"),
            product_name="Detergente",
        )

        assert allocations[0].unit_price == Decimal("10.00")
        assert allocations[1].unit_price == Decimal("12.00")
        assert sale_total(allocations) == Decimal("32.00")

    def test_a_lot_kept_for_internal_use_is_skipped(self) -> None:
        """No sale price means the laundry's own supply — not a free bottle."""
        allocations = allocate_fifo(
            [lot(INTERNAL, 1, "50.00", None), lot(NEWER, 2, "5.00", "12.00")],
            Decimal("4.00"),
            product_name="Suavizante",
        )

        assert [allocation.lot_number for allocation in allocations] == [2]

    def test_an_empty_lot_is_skipped(self) -> None:
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "0.00"), lot(NEWER, 8, "3.00")],
            Decimal("3.00"),
            product_name="Cloro",
        )

        assert [allocation.lot_number for allocation in allocations] == [8]

    def test_not_enough_stock_says_how_much_there_is(self) -> None:
        with pytest.raises(InsufficientStock) as error:
            allocate_fifo(
                [lot(OLDEST, 7, "2.00"), lot(INTERNAL, 8, "40.00", None)],
                Decimal("5.00"),
                product_name="Detergente",
            )

        # The internal lot does not count towards what could be sold, and both
        # numbers travel with the error: the review queue of PR 11 shows them
        # side by side (D12).
        assert error.value.requested == Decimal("5.00")
        assert error.value.available == Decimal("2.00")
        assert "Detergente" in str(error.value)

    def test_nothing_is_allocated_when_the_stock_falls_short(self) -> None:
        """The check runs before the loop, so a sale is never half-covered."""
        with pytest.raises(InsufficientStock):
            allocate_fifo([lot(OLDEST, 7, "2.00")], Decimal("3.00"), product_name="Cloro")

    def test_a_line_of_zero_is_refused(self) -> None:
        with pytest.raises(ConflictError):
            allocate_fifo([lot(OLDEST, 7, "5.00")], Decimal("0.00"), product_name="Cloro")

    def test_a_negative_line_is_refused(self) -> None:
        with pytest.raises(ConflictError):
            allocate_fifo([lot(OLDEST, 7, "5.00")], Decimal("-1.00"), product_name="Cloro")

    def test_an_empty_shelf_is_insufficient_and_not_an_empty_sale(self) -> None:
        with pytest.raises(InsufficientStock):
            allocate_fifo([], Decimal("1.00"), product_name="Cloro")


class TestMoney:
    def test_a_fraction_of_a_unit_rounds_half_up(self) -> None:
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "10.00", "3.33")], Decimal("1.50"), product_name="Cloro"
        )

        # 1.50 x 3.33 = 4.995, and a person rounding on paper writes 5.00.
        assert allocations[0].amount == Decimal("5.00")

    def test_the_total_adds_up_the_printed_lines(self) -> None:
        """Summing the rounded amounts, not the exact products (`core.money`).

        A customer checking the receipt by hand adds what is printed, so the
        total has to be that sum and not a cent away from it.
        """
        allocations = allocate_fifo(
            [lot(OLDEST, 7, "1.00", "3.33"), lot(NEWER, 8, "1.00", "3.33")],
            Decimal("2.00"),
            product_name="Cloro",
        )

        assert [allocation.amount for allocation in allocations] == [
            Decimal("3.33"),
            Decimal("3.33"),
        ]
        assert sale_total(allocations) == Decimal("6.66")

    def test_a_sale_of_nothing_totals_zero(self) -> None:
        assert sale_total([]) == Decimal("0.00")


class TestAvailableStock:
    def test_only_what_could_be_sold_counts(self) -> None:
        lots = [lot(OLDEST, 7, "2.00"), lot(INTERNAL, 8, "40.00", None), lot(NEWER, 9, "0.00")]

        assert available_stock(lots) == Decimal("2.00")

    def test_an_empty_shelf_is_zero_and_not_none(self) -> None:
        assert available_stock([]) == Decimal("0.00")
