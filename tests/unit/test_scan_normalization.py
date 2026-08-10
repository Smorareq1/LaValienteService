"""What the module believes of what the model read (Plan 0003 §7).

No network and no database: this is the deterministic half, and it is the half
that decides whether a reading becomes a usable draft or a mess somebody has to
undo. The fixtures are readings, written the way Gemini returns them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from src.modules.catalog.models import GarmentType
from src.modules.intake_scan.matching import NAME_THRESHOLD, best_match, name_similarity
from src.modules.intake_scan.normalization import (
    check_total,
    check_weight,
    normalize,
    normalize_nit,
    normalize_phone,
    resolve_charges,
    resolve_date,
    resolve_garments,
)
from src.modules.intake_scan.schemas import RawScan

TODAY = date(2026, 8, 8)


def leaf(value: Any, confidence: float = 0.95, raw: str | None = None) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "raw_text": raw}


def garment_type(name: str) -> GarmentType:
    kind = GarmentType(name=name, is_active=True)
    kind.id = uuid4()
    return kind


#: The 21 of the printed ticket, as `seed_catalog` writes them.
GARMENTS = [
    garment_type(name)
    for name in (
        "Blusa",
        "Camisa",
        "Pantalón",
        "Pants/Pijama",
        "Playera/Polo",
        "Toalla grande",
    )
]


def reading(**overrides: Any) -> RawScan:
    payload: dict[str, Any] = {
        "header": {
            "date": {"day": leaf(8), "month": leaf(8), "year": leaf(2026)},
            "daily_number": leaf(41),
            "booklet_serial": leaf("A-4410"),
            "weight_lbs": leaf(12.5),
            "nit": leaf("CF"),
        },
        "customer": {
            "full_name": leaf("María López"),
            "phone": leaf("5512-3456"),
            "address": leaf(None, 0.0),
            "email": leaf(None, 0.0),
        },
        "garments": [],
        "observations": leaf(None, 0.0),
        "services": {
            "wash_by_weight_lbs": leaf(12.5),
            "wash_tub": {},
            "dry": {},
            "hand_wash": {},
            "extras": {},
            "pickup_amount": leaf(None, 0.0),
            "delivery_amount": leaf(None, 0.0),
        },
        "discounts_marked": [],
        "totals_read": {
            "subtotal": leaf(None, 0.0),
            "discount": leaf(None, 0.0),
            "total": leaf(None, 0.0),
        },
    }
    payload.update(overrides)
    return RawScan.model_validate(payload)


class TestThePhone:
    """§7.1 — eight digits or nothing."""

    def test_it_strips_the_punctuation_people_write(self) -> None:
        assert normalize_phone("5512-3456") == "55123456"
        assert normalize_phone("5512 3456") == "55123456"

    def test_the_country_code_is_dropped(self) -> None:
        """`+502 5512 3456` and `55123456` must not look like two people."""
        assert normalize_phone("+502 5512 3456") == "55123456"

    def test_a_half_read_number_comes_back_empty(self) -> None:
        """Worse than blank: a wrong number gets dialled."""
        assert normalize_phone("5512") is None
        assert normalize_phone("551234567890") is None
        assert normalize_phone(None) is None


class TestTheNit:
    def test_cf_is_a_value_and_not_a_blank(self) -> None:
        assert normalize_nit("cf") == "CF"
        assert normalize_nit("C/F") == "CF"

    def test_digits_keep_their_check_character(self) -> None:
        assert normalize_nit("1234567-K") == "1234567-K"
        assert normalize_nit("1234567k") == "1234567-K"

    def test_a_plain_number_needs_no_dash(self) -> None:
        assert normalize_nit("12345678") == "12345678"

    def test_nonsense_is_refused(self) -> None:
        assert normalize_nit("ABC") is None
        assert normalize_nit("12") is None


class TestTheDate:
    def test_a_blank_box_means_today(self) -> None:
        """The talonario prints Xs where nothing was written — that is normal."""
        raw = reading(
            header={
                "date": {"day": leaf(None, 0.0), "month": leaf(None, 0.0), "year": leaf(None, 0.0)},
                "daily_number": leaf(41),
                "booklet_serial": leaf("A-4410"),
                "weight_lbs": leaf(12.5),
                "nit": leaf("CF"),
            }
        )
        resolved, warnings = resolve_date(raw, TODAY)

        assert resolved.value == TODAY
        assert warnings == []

    def test_an_impossible_date_falls_back_and_says_so(self) -> None:
        raw = reading(
            header={
                "date": {"day": leaf(31), "month": leaf(2), "year": leaf(2026)},
                "daily_number": leaf(41),
                "booklet_serial": leaf("A-4410"),
                "weight_lbs": leaf(12.5),
                "nit": leaf("CF"),
            }
        )
        resolved, warnings = resolve_date(raw, TODAY)

        assert resolved.value == TODAY
        assert warnings == ["date_unreadable:2026-2-31"]

    def test_a_date_far_from_today_is_flagged_not_refused(self) -> None:
        """Usually a misread year, and the person can see the photo."""
        raw = reading(
            header={
                "date": {"day": leaf(8), "month": leaf(8), "year": leaf(2020)},
                "daily_number": leaf(41),
                "booklet_serial": leaf("A-4410"),
                "weight_lbs": leaf(12.5),
                "nit": leaf("CF"),
            }
        )
        resolved, warnings = resolve_date(raw, TODAY)

        assert resolved.value == date(2020, 8, 8)
        assert warnings == ["date_far_from_today:2020-08-08"]


class TestTheGarments:
    def test_accents_and_case_do_not_matter(self) -> None:
        raw = reading(garments=[{"name": leaf("pantalon"), "quantity": leaf(3)}])

        resolved, warnings = resolve_garments(raw, GARMENTS)

        assert [(line.name, line.quantity) for line in resolved] == [("Pantalón", 3)]
        assert warnings == []

    def test_half_of_a_compound_name_finds_the_whole(self) -> None:
        """People write «Pijama», the ticket prints «Pants/Pijama»."""
        raw = reading(garments=[{"name": leaf("Pijama"), "quantity": leaf(2)}])

        resolved, _ = resolve_garments(raw, GARMENTS)

        assert [line.name for line in resolved] == ["Pants/Pijama"]

    def test_an_unmatched_garment_is_dropped_with_a_warning(self) -> None:
        """Never guessed at: it would put clothes on a ticket nobody brought."""
        raw = reading(garments=[{"name": leaf("Sombrero"), "quantity": leaf(1)}])

        resolved, warnings = resolve_garments(raw, GARMENTS)

        assert resolved == []
        assert warnings == ["garment_unmatched:Sombrero"]

    def test_a_row_without_a_quantity_is_not_a_line(self) -> None:
        raw = reading(garments=[{"name": leaf("Camisa"), "quantity": leaf(None, 0.0)}])

        resolved, _ = resolve_garments(raw, GARMENTS)

        assert resolved == []

    def test_a_shaky_reading_is_marked_for_review(self) -> None:
        raw = reading(garments=[{"name": leaf("Camisa", 0.6), "quantity": leaf(3, 0.7)}])

        resolved, _ = resolve_garments(raw, GARMENTS)

        assert resolved[0].needs_review is True


class TestTheServices:
    def test_the_boxes_become_charges_with_no_prices(self) -> None:
        """D4: what is read is what and how many. Money comes from the catalog."""
        raw = reading(
            services={
                "wash_by_weight_lbs": leaf(12.5),
                "wash_tub": {"G": leaf(2)},
                "dry": {"T50": leaf(1)},
                "hand_wash": {},
                "extras": {"rins": leaf(1)},
                "pickup_amount": leaf(None, 0.0),
                "delivery_amount": leaf(None, 0.0),
            }
        )

        charges, confidences, warnings = resolve_charges(raw)

        assert [(line.service_code, line.option_code, line.quantity) for line in charges] == [
            ("wash_by_weight", None, Decimal("12.5")),
            ("wash_tub", "G", Decimal(2)),
            ("dry", "T50", Decimal(1)),
            ("extra_softener", None, Decimal(1)),
        ]
        assert all(line.amount is None for line in charges)
        assert len(confidences) == len(charges)
        assert warnings == []

    def test_an_empty_box_is_not_a_zero(self) -> None:
        """A zero says «se pidió cero»; the box says nothing was asked for."""
        raw = reading(
            services={
                "wash_by_weight_lbs": leaf(None, 0.0),
                "wash_tub": {"G": leaf(0), "E": leaf(None, 0.0)},
                "dry": {},
                "hand_wash": {},
                "extras": {},
                "pickup_amount": leaf(None, 0.0),
                "delivery_amount": leaf(None, 0.0),
            }
        )

        charges, _, warnings = resolve_charges(raw)

        assert charges == []
        assert warnings == ["no_services_read"]

    def test_pickup_and_delivery_carry_the_amount_the_courier_wrote(self) -> None:
        """The two exceptions to D4: no catalog holds these."""
        raw = reading(
            services={
                "wash_by_weight_lbs": leaf(None, 0.0),
                "wash_tub": {},
                "dry": {},
                "hand_wash": {},
                "extras": {},
                "pickup_amount": leaf(25),
                "delivery_amount": leaf(30),
            }
        )

        charges, _, _ = resolve_charges(raw)

        assert [(line.service_code, line.amount) for line in charges] == [
            ("pickup", Decimal(25)),
            ("delivery", Decimal(30)),
        ]


class TestTheWeight:
    def test_the_header_follows_the_billed_pounds(self) -> None:
        """The engine refuses a mismatch; here that would be a dead end, so the
        disagreement is reported and the capture goes on."""
        raw = reading(
            header={
                "date": {"day": leaf(8), "month": leaf(8), "year": leaf(2026)},
                "daily_number": leaf(41),
                "booklet_serial": leaf("A-4410"),
                "weight_lbs": leaf(12.5),
                "nit": leaf("CF"),
            },
            services={
                "wash_by_weight_lbs": leaf(21.5),
                "wash_tub": {},
                "dry": {},
                "hand_wash": {},
                "extras": {},
                "pickup_amount": leaf(None, 0.0),
                "delivery_amount": leaf(None, 0.0),
            },
        )
        normalized = normalize(raw, today=TODAY, garment_types=GARMENTS)

        assert normalized.weight_lbs.value == Decimal("21.5")
        assert "weight_mismatch:12.5:21.5" in normalized.warnings

    def test_no_by_weight_line_leaves_the_header_alone(self) -> None:
        weight, warnings = check_weight(Decimal("12.5"), [])

        assert weight == Decimal("12.5")
        assert warnings == []


class TestTheTotal:
    def test_a_gap_over_a_quetzal_is_reported(self) -> None:
        """§7.4 — almost always a quantity read wrong, which is worth pointing at."""
        assert check_total(Decimal("108.75"), Decimal("98.75")) == [
            "total_mismatch:108.75:98.75"
        ]

    def test_rounding_is_not_a_discrepancy(self) -> None:
        assert check_total(Decimal("98.75"), Decimal("98.00")) == []

    def test_nothing_read_is_nothing_to_compare(self) -> None:
        assert check_total(None, Decimal("98.75")) == []


class TestTheCustomerMatch:
    """§7.5 — a suggestion, never a decision, and never an auto-creation."""

    class FakeCustomer:
        def __init__(self, name: str, phone: str | None = None) -> None:
            self.id = uuid4()
            self.full_name = name
            self.phone = phone
            self.created_at = date(2026, 1, 1)

    def test_word_order_does_not_make_two_people(self) -> None:
        assert name_similarity("María López", "López María") > NAME_THRESHOLD

    def test_accents_do_not_either(self) -> None:
        assert name_similarity("Maria Lopez", "María López") == 1.0

    def test_the_phone_wins_when_there_is_one(self) -> None:
        """The only real identifier on the ticket: two people share a name."""
        by_phone = self.FakeCustomer("María López", "55123456")

        match = best_match(
            phone="55123456", full_name="Otra Persona", by_phone=by_phone, candidates=[]
        )

        assert match is not None
        assert match.matched_on == "phone"
        assert match.score == 1.0

    def test_a_distant_name_is_not_suggested(self) -> None:
        """Accepting a wrong suggestion in a hurry costs more than retyping."""
        match = best_match(
            phone=None,
            full_name="María López",
            by_phone=None,
            candidates=[self.FakeCustomer("Juan Pérez")],
        )

        assert match is None


class TestTheWholeReading:
    def test_a_clean_ticket_normalizes_end_to_end(self) -> None:
        raw = reading(garments=[{"name": leaf("Camisa"), "quantity": leaf(3)}])

        normalized = normalize(raw, today=TODAY, garment_types=GARMENTS)

        assert normalized.order_date.value == TODAY
        assert normalized.booklet_serial.value == "A-4410"
        assert normalized.nit.value == "CF"
        assert normalized.customer_phone.value == "55123456"
        assert [line.name for line in normalized.garments] == ["Camisa"]
        assert normalized.warnings == []

    def test_an_unreadable_phone_is_dropped_and_reported(self) -> None:
        raw = reading(
            customer={
                "full_name": leaf("María López"),
                "phone": leaf("551"),
                "address": leaf(None, 0.0),
                "email": leaf(None, 0.0),
            }
        )

        normalized = normalize(raw, today=TODAY, garment_types=GARMENTS)

        assert normalized.customer_phone.value is None
        assert "phone_unreadable:551" in normalized.warnings
