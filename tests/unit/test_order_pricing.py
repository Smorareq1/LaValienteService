"""The order total — the number the customer pays (Plan 0001 §6).

Everything here is pure arithmetic over an in-memory catalog: no session, no
HTTP. If one of these breaks, someone is being charged the wrong amount.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.business_time import GUATEMALA, business_date
from src.core.exceptions import ConflictError
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.orders.pricing import (
    ChargeRequest,
    DiscountRequest,
    PriceBook,
    money,
    price_order,
)

ORDER_DATE = date(2026, 7, 20)


def service(
    code: str,
    name: str,
    mode: PricingMode,
    options: list[tuple[str, str, int | None, int | None]] | None = None,
) -> ServiceType:
    built = ServiceType(code=code, name=name, pricing_mode=mode)
    built.id = uuid4()
    built.options = []
    for option_code, option_name, low, high in options or []:
        option = ServiceOption(
            code=option_code, name=option_name, min_quantity=low, max_quantity=high
        )
        option.id = uuid4()
        option.is_active = True
        option.deleted_at = None
        built.options.append(option)
    return built


def price(service_type: ServiceType, amount: str, option_code: str | None = None) -> ServicePrice:
    option = next((o for o in service_type.options if o.code == option_code), None)
    return ServicePrice(
        service_type_id=service_type.id,
        service_option_id=option.id if option is not None else None,
        price=Decimal(amount),
        valid_from=date(2026, 1, 1),
        valid_to=None,
    )


def catalog() -> tuple[list[ServiceType], list[ServicePrice]]:
    """The real seeded catalog, trimmed to what these tests bill."""
    by_weight = service("wash_by_weight", "Lavado por peso", PricingMode.PER_UNIT)
    tub = service(
        "wash_tub",
        "Lavado por tina",
        PricingMode.TIERED,
        [("G", "Tina grande", None, None), ("E", "Tina estándar", None, None)],
    )
    dry = service(
        "dry",
        "Secado",
        PricingMode.TIERED,
        [("T40", "Secado 40 min", None, None), ("T60", "Secado 60 min", None, None)],
    )
    hand_wash = service(
        "hand_wash",
        "Lavado a mano",
        PricingMode.TIERED,
        [("N2", "Nivel 2", 1, 4), ("N3", "Nivel 3", 5, 9)],
    )
    softener = service("extra_softener", "Rins (suavizante)", PricingMode.PER_UNIT)
    delivery = service("delivery", "Entrega a domicilio", PricingMode.VARIABLE)

    services = [by_weight, tub, dry, hand_wash, softener, delivery]
    prices = [
        price(by_weight, "2.50"),
        price(tub, "30.00", "G"),
        price(tub, "25.00", "E"),
        price(dry, "20.00", "T40"),
        price(dry, "30.00", "T60"),
        price(hand_wash, "5.00", "N2"),
        price(hand_wash, "10.00", "N3"),
        price(softener, "10.00"),
    ]
    return services, prices


def book(on_date: date = ORDER_DATE) -> PriceBook:
    services, prices = catalog()
    return PriceBook(services, prices, on_date)


class TestTheWorkedExample:
    """§6.4 — the ticket the plan does by hand, done by the engine."""

    def test_the_plans_own_ticket_adds_up(self) -> None:
        priced = price_order(
            [
                ChargeRequest("wash_by_weight", quantity=Decimal("12.5")),
                ChargeRequest("wash_tub", option_code="G"),
                ChargeRequest("dry", option_code="T60"),
                ChargeRequest("extra_softener"),
                ChargeRequest("delivery", amount=Decimal("15.00")),
            ],
            [DiscountRequest("50% domicilio", Decimal("7.50"))],
            book(),
            weight_lbs=Decimal("12.5"),
        )

        assert [line.amount for line in priced.charges] == [
            Decimal("31.25"),
            Decimal("30.00"),
            Decimal("30.00"),
            Decimal("10.00"),
            Decimal("15.00"),
        ]
        assert priced.subtotal == Decimal("116.25")
        assert priced.discount_total == Decimal("7.50")
        assert priced.total == Decimal("108.75")

    def test_the_second_discount_of_the_example(self) -> None:
        """Adding the July duvet promo: (Q30 + Q30) - Q35 = Q25 off."""
        priced = price_order(
            [
                ChargeRequest("wash_by_weight", quantity=Decimal("12.5")),
                ChargeRequest("wash_tub", option_code="G"),
                ChargeRequest("dry", option_code="T60"),
                ChargeRequest("extra_softener"),
                ChargeRequest("delivery", amount=Decimal("15.00")),
            ],
            [
                DiscountRequest("50% domicilio", Decimal("7.50")),
                DiscountRequest("Promo edredón julio", Decimal("25.00")),
            ],
            book(),
            weight_lbs=Decimal("12.5"),
        )

        assert priced.total == Decimal("83.75")


class TestCharges:
    def test_every_service_bills_n_times_its_price(self) -> None:
        """D3.1: three softeners is one line with quantity 3, not three lines."""
        priced = price_order(
            [ChargeRequest("extra_softener", quantity=Decimal(3))], [], book()
        )

        assert priced.charges[0].quantity == Decimal(3)
        assert priced.charges[0].unit_price == Decimal("10.00")
        assert priced.subtotal == Decimal("30.00")

    def test_two_large_tubs(self) -> None:
        priced = price_order(
            [ChargeRequest("wash_tub", option_code="G", quantity=Decimal(2))], [], book()
        )

        assert priced.subtotal == Decimal("60.00")

    def test_the_line_carries_the_option_name(self) -> None:
        """The description is a snapshot (D2): it has to read on its own later."""
        priced = price_order([ChargeRequest("dry", option_code="T60")], [], book())

        assert priced.charges[0].description == "Secado — Secado 60 min"

    def test_a_variable_service_takes_the_amount_that_was_typed(self) -> None:
        priced = price_order([ChargeRequest("delivery", amount=Decimal("22.75"))], [], book())

        assert priced.charges[0].amount == Decimal("22.75")
        assert priced.charges[0].service_option_id is None

    def test_a_variable_service_without_an_amount_is_refused(self) -> None:
        with pytest.raises(ConflictError, match="amount"):
            price_order([ChargeRequest("delivery")], [], book())

    def test_a_catalogued_service_may_not_carry_its_own_price(self) -> None:
        """D5: the client sends quantities, never money."""
        with pytest.raises(ConflictError, match="no amount"):
            price_order(
                [ChargeRequest("extra_softener", amount=Decimal("999.00"))], [], book()
            )

    def test_a_tiered_service_needs_its_option(self) -> None:
        with pytest.raises(ConflictError, match="option"):
            price_order([ChargeRequest("wash_tub")], [], book())

    def test_a_per_unit_service_takes_no_option(self) -> None:
        with pytest.raises(ConflictError, match="no option"):
            price_order([ChargeRequest("extra_softener", option_code="G")], [], book())

    def test_an_unknown_service_is_refused(self) -> None:
        with pytest.raises(ConflictError, match="ironing"):
            price_order([ChargeRequest("ironing")], [], book())

    def test_an_option_of_another_service_is_refused(self) -> None:
        with pytest.raises(ConflictError, match="T60"):
            price_order([ChargeRequest("wash_tub", option_code="T60")], [], book())

    def test_an_order_needs_at_least_one_charge(self) -> None:
        with pytest.raises(ConflictError, match="at least one"):
            price_order([], [], book())

    def test_a_service_with_no_price_that_day_does_not_cost_zero(self) -> None:
        """The worst possible failure: a ticket that quietly bills nothing."""
        services, _ = catalog()
        empty = PriceBook(services, [], ORDER_DATE)

        with pytest.raises(ConflictError, match="no price in force"):
            price_order([ChargeRequest("extra_softener")], [], empty)


class TestRounding:
    def test_each_line_is_rounded_before_the_sum(self) -> None:
        """So the printed lines add up to the printed subtotal.

        12.33 lb x Q2.50 = Q30.825 exactly; the ticket says Q30.83 and that is
        what the subtotal has to agree with.
        """
        priced = price_order(
            [ChargeRequest("wash_by_weight", quantity=Decimal("12.33"))],
            [],
            book(),
            weight_lbs=Decimal("12.33"),
        )

        assert priced.charges[0].amount == Decimal("30.83")
        assert priced.subtotal == Decimal("30.83")

    def test_half_a_cent_rounds_up(self) -> None:
        assert money(Decimal("0.125")) == Decimal("0.13")
        assert money(Decimal("0.135")) == Decimal("0.14")


class TestDiscounts:
    def test_a_discount_may_not_exceed_the_subtotal(self) -> None:
        """§6.2 step 4: a ticket that owes the customer money is a typo."""
        with pytest.raises(ConflictError, match="exceed the subtotal"):
            price_order(
                [ChargeRequest("extra_softener")],
                [DiscountRequest("De más", Decimal("11.00"))],
                book(),
            )

    def test_a_discount_equal_to_the_subtotal_leaves_zero(self) -> None:
        priced = price_order(
            [ChargeRequest("extra_softener")],
            [DiscountRequest("Cortesía", Decimal("10.00"))],
            book(),
        )

        assert priced.total == Decimal("0.00")

    def test_discounts_add_up(self) -> None:
        priced = price_order(
            [ChargeRequest("wash_tub", option_code="G")],
            [
                DiscountRequest("Uno", Decimal("5.00")),
                DiscountRequest("Otro", Decimal("2.50")),
            ],
            book(),
        )

        assert priced.discount_total == Decimal("7.50")
        assert priced.total == Decimal("22.50")

    def test_a_manual_discount_carries_no_promotion(self) -> None:
        """PR 5 fills this in; until then every discount is somebody's decision."""
        priced = price_order(
            [ChargeRequest("wash_tub", option_code="G")],
            [DiscountRequest("Cliente frecuente", Decimal("5.00"))],
            book(),
        )

        assert priced.discounts[0].promotion_id is None


class TestWeight:
    def test_the_header_weight_must_match_the_pounds_billed(self) -> None:
        """Captured twice; letting them drift bills 21 lb on a 12 lb ticket."""
        with pytest.raises(ConflictError, match="does not match"):
            price_order(
                [ChargeRequest("wash_by_weight", quantity=Decimal("21"))],
                [],
                book(),
                weight_lbs=Decimal("12"),
            )

    def test_washing_by_weight_demands_a_weight(self) -> None:
        with pytest.raises(ConflictError, match="weight in pounds"):
            price_order([ChargeRequest("wash_by_weight", quantity=Decimal("12"))], [], book())

    def test_a_ticket_with_no_weight_line_needs_no_weight(self) -> None:
        priced = price_order([ChargeRequest("wash_tub", option_code="E")], [], book())

        assert priced.total == Decimal("25.00")


class TestHandWashLevels:
    def test_a_level_outside_its_range_warns_without_blocking(self) -> None:
        """§6.3: the paper ticket settled this with judgement, and so does this."""
        priced = price_order(
            [ChargeRequest("hand_wash", option_code="N2")], [], book(), total_pieces=12
        )

        assert priced.total == Decimal("5.00")
        assert len(priced.warnings) == 1
        assert "N2" in priced.warnings[0]
        assert "12" in priced.warnings[0]

    def test_a_level_within_its_range_says_nothing(self) -> None:
        priced = price_order(
            [ChargeRequest("hand_wash", option_code="N3")], [], book(), total_pieces=7
        )

        assert priced.warnings == []


class TestBusinessDate:
    def test_the_evening_in_coban_is_still_today(self) -> None:
        """D8: 19:00 in Cobán is 01:00 UTC tomorrow, and it is not tomorrow's ticket."""
        from datetime import UTC, datetime

        late_evening = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)

        assert business_date(late_evening) == date(2026, 7, 20)

    def test_the_offset_is_the_one_guatemala_uses(self) -> None:
        from datetime import timedelta

        assert GUATEMALA.utcoffset(None) == timedelta(hours=-6)
