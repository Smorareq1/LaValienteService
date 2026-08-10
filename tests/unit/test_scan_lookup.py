"""Finding a ticket that already exists (Plan 0006 §7.1.1).

The other half of the module. `test_scan_normalization` covers turning a reading
into a draft; this covers turning one into an *answer to which ticket is this*,
which fails differently: a draft that is wrong gets corrected by the person
looking at it, and a lookup that is wrong hands back somebody else's clothes.

No provider and no database. The extractor is a stub returning a reading, and
the repositories are fakes — what is under test is the resolution, which is
where the ticket is chosen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.modules.intake_scan.extractor import Extraction, ScanFailure
from src.modules.intake_scan.models import ScanJob, ScanPurpose, ScanStatus
from src.modules.intake_scan.normalization import normalize_serial
from src.modules.intake_scan.schemas import (
    MATCHED_ON_BOOKLET_SERIAL,
    MATCHED_ON_DAILY_NUMBER,
    RawScan,
)
from src.modules.intake_scan.service import ScanService
from src.modules.orders.models import OrderStatus

TODAY = date(2026, 8, 8)

#: A one-pixel JPEG. Nothing here inspects it — the extractor is a stub — but the
#: service takes bytes and the shape should be honest.
JPEG = bytes.fromhex("ffd8ffe000104a464946000101" + "00" * 8 + "ffd9")


def leaf(value: Any, confidence: float = 0.95, raw: str | None = None) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "raw_text": raw}


def reading(
    *,
    daily_number: dict[str, Any] | None = None,
    booklet_serial: dict[str, Any] | None = None,
    on: date = TODAY,
) -> RawScan:
    """A reading of the header, which is all a lookup ever looks at."""
    return RawScan.model_validate(
        {
            "header": {
                "date": {
                    "day": leaf(on.day),
                    "month": leaf(on.month),
                    "year": leaf(on.year),
                },
                "daily_number": daily_number or leaf(None, 0.0),
                "booklet_serial": booklet_serial or leaf(None, 0.0),
            },
            "customer": {},
            "garments": [],
            "services": {},
        }
    )


# ------------------------------------------------------------------ los dobles


@dataclass
class FakeOrder:
    """Only what a match is built from."""

    id: UUID = field(default_factory=uuid4)
    order_date: date = TODAY
    daily_number: int = 41
    booklet_serial: str | None = None
    customer_id: UUID = field(default_factory=uuid4)
    status: OrderStatus = OrderStatus.RECEIVED
    total_pieces: int = 3
    total: Decimal = Decimal("120.00")
    paid_total: Decimal = Decimal("0.00")
    balance: Decimal = Decimal("120.00")


class FakeOrdersRepository:
    """Answers the two identifiers the way the real query does, unioned."""

    def __init__(self, orders: list[FakeOrder] | None = None) -> None:
        self.orders = orders or []
        self.calls: list[dict[str, Any]] = []

    async def find_by_ticket(
        self,
        *,
        booklet_serial: str | None = None,
        order_date: date | None = None,
        daily_number: int | None = None,
    ) -> list[FakeOrder]:
        self.calls.append(
            {
                "booklet_serial": booklet_serial,
                "order_date": order_date,
                "daily_number": daily_number,
            }
        )
        found: list[FakeOrder] = []
        for order in self.orders:
            by_serial = booklet_serial is not None and (
                normalize_serial(order.booklet_serial) == booklet_serial
            )
            by_number = (
                order_date is not None
                and daily_number is not None
                and order.order_date == order_date
                and order.daily_number == daily_number
            )
            if by_serial or by_number:
                found.append(order)
        return found


class FakeScanRepository:
    def __init__(self) -> None:
        self.jobs: list[ScanJob] = []
        self.today_count = 0

    def add(self, job: ScanJob) -> None:
        self.jobs.append(job)

    async def flush(self) -> None: ...

    async def commit(self) -> None: ...

    async def count_today(self) -> int:
        return self.today_count


class StubExtractor:
    """A reading, or a provider that is down."""

    def __init__(self, payload: dict[str, Any] | None = None, fails: bool = False) -> None:
        self.payload = payload or {}
        self.fails = fails

    async def extract(self, image: bytes) -> Extraction:
        if self.fails:
            raise ScanFailure("The scan provider could not be reached.")
        return Extraction(
            payload=self.payload, model="gemini-2.5-flash", prompt_version="v1", latency_ms=812
        )


def build(
    orders: list[FakeOrder] | None = None,
    extractor: StubExtractor | None = None,
) -> tuple[ScanService, FakeScanRepository, FakeOrdersRepository]:
    scans = FakeScanRepository()
    repo = FakeOrdersRepository(orders)
    service = ScanService(
        scans,  # type: ignore[arg-type]
        extractor or StubExtractor(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
    )
    return service, scans, repo


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the business date: `resolve_date` falls back to it for a blank box."""
    monkeypatch.setattr("src.modules.intake_scan.service.business_date", lambda: TODAY)


@pytest.fixture(autouse=True)
def _scan_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "scan_daily_limit", 0, raising=False)
    monkeypatch.setattr(settings, "scan_model", "gemini-2.5-flash", raising=False)
    monkeypatch.setattr(settings, "scan_prompt_version", "v1", raising=False)


# ------------------------------------------------------------ el serial impreso


class TestNormalizingTheSerial:
    """It is compared with `==`, so both sides have to be spelled the same way."""

    @pytest.mark.parametrize(
        "written",
        ["A-0042", " a-0042 ", "#A-0042", "#a-0042", "A-0042\n"],
    )
    def test_the_ways_one_serial_gets_written_are_one_serial(self, written: str) -> None:
        assert normalize_serial(written) == "A-0042"

    def test_the_hyphen_is_left_alone(self) -> None:
        """What the print shop puts on the sheet is the identifier; this is not
        the place to decide which of its characters count."""
        assert normalize_serial("A-0042") != normalize_serial("A0042")

    def test_nothing_written_is_nothing(self) -> None:
        assert normalize_serial(None) is None
        assert normalize_serial("   ") is None
        assert normalize_serial("#") is None


# ----------------------------------------------------------------- resolviendo


class TestResolvingTheTicket:
    async def test_the_printed_serial_names_the_ticket(self) -> None:
        order = FakeOrder(booklet_serial="A-0042", daily_number=41)
        service, _, _ = build([order])

        resolved = await service._resolve_ticket(
            reading(booklet_serial=leaf("A-0042"))
        )

        assert [match.order_id for match in resolved.matches] == [order.id]
        assert resolved.matches[0].matched_on == MATCHED_ON_BOOKLET_SERIAL
        assert resolved.warnings == []

    async def test_a_serial_read_with_the_hash_still_finds_it(self) -> None:
        """The paper prints `#Tomapedido`, so the model reads the `#` too."""
        order = FakeOrder(booklet_serial="A-0042")
        service, _, _ = build([order])

        resolved = await service._resolve_ticket(
            reading(booklet_serial=leaf("#a-0042"))
        )

        assert [match.order_id for match in resolved.matches] == [order.id]

    async def test_the_handwritten_number_needs_its_date(self) -> None:
        """`daily_number` is only unique within its day, so the pair travels
        together or not at all."""
        order = FakeOrder(daily_number=41, order_date=TODAY)
        service, _, repo = build([order])

        resolved = await service._resolve_ticket(reading(daily_number=leaf(41)))

        assert [match.order_id for match in resolved.matches] == [order.id]
        assert resolved.matches[0].matched_on == MATCHED_ON_DAILY_NUMBER
        assert repo.calls[0]["order_date"] == TODAY

    async def test_a_blank_date_box_means_today(self) -> None:
        """The talonario prints Xs where nothing was written, and the tickets
        handed back at closing are the ones taken that morning."""
        service, _, repo = build([FakeOrder(daily_number=41)])
        blank = RawScan.model_validate(
            {
                "header": {"date": {}, "daily_number": leaf(41), "booklet_serial": leaf(None, 0.0)},
                "customer": {},
                "garments": [],
                "services": {},
            }
        )

        resolved = await service._resolve_ticket(blank)

        assert repo.calls[0]["order_date"] == TODAY
        assert len(resolved.matches) == 1

    async def test_an_illegible_reading_asks_for_nothing(self) -> None:
        """A guess that resolves to a real ticket is worse than no answer."""
        service, _, repo = build([FakeOrder(daily_number=41)])

        resolved = await service._resolve_ticket(
            reading(daily_number=leaf(41, confidence=0.20))
        )

        assert resolved.matches == []
        assert resolved.warnings == ["ticket_unreadable"]
        # And the database was never asked: there was nothing to ask it.
        assert repo.calls == []

    async def test_a_number_that_is_not_here_says_which_number(self) -> None:
        """Usually a misread digit, and showing it is what lets somebody
        correct it by typing."""
        service, _, _ = build([FakeOrder(daily_number=41)])

        resolved = await service._resolve_ticket(reading(daily_number=leaf(9)))

        assert resolved.matches == []
        assert resolved.warnings == ["no_match:9"]

    async def test_a_serial_and_a_number_that_disagree_are_both_offered(self) -> None:
        """That disagreement is the useful answer; picking one would hide it."""
        by_serial = FakeOrder(booklet_serial="A-0042", daily_number=7)
        by_number = FakeOrder(booklet_serial="A-0099", daily_number=41)
        service, _, _ = build([by_serial, by_number])

        resolved = await service._resolve_ticket(
            reading(booklet_serial=leaf("A-0042"), daily_number=leaf(41))
        )

        assert {match.order_id for match in resolved.matches} == {by_serial.id, by_number.id}
        assert resolved.warnings == ["serial_and_number_disagree"]

    async def test_one_ticket_found_by_both_is_one_match(self) -> None:
        order = FakeOrder(booklet_serial="A-0042", daily_number=41)
        service, _, _ = build([order])

        resolved = await service._resolve_ticket(
            reading(booklet_serial=leaf("A-0042"), daily_number=leaf(41))
        )

        assert len(resolved.matches) == 1
        # Found by both, reported by the stronger one: the serial is printed.
        assert resolved.matches[0].matched_on == MATCHED_ON_BOOKLET_SERIAL
        assert resolved.warnings == []

    async def test_an_already_delivered_ticket_is_still_an_answer(self) -> None:
        """"That one was handed back on Tuesday" is an answer; finding nothing
        is not."""
        order = FakeOrder(daily_number=41, status=OrderStatus.DELIVERED)
        service, _, _ = build([order])

        resolved = await service._resolve_ticket(reading(daily_number=leaf(41)))

        assert resolved.matches[0].status == OrderStatus.DELIVERED

    async def test_the_balance_travels_so_the_counter_can_settle_it(self) -> None:
        order = FakeOrder(
            daily_number=41, total=Decimal("120.00"), paid_total=Decimal("50.00"),
            balance=Decimal("70.00"),
        )
        service, _, _ = build([order])

        match = (await service._resolve_ticket(reading(daily_number=leaf(41)))).matches[0]

        assert match.total == Decimal("120.00")
        assert match.paid_total == Decimal("50.00")
        assert match.balance == Decimal("70.00")

    async def test_what_it_read_comes_back_with_the_answer(self) -> None:
        """A match the counter disagrees with is only arguable if the reading
        is visible."""
        service, _, _ = build([FakeOrder(daily_number=41)])

        resolved = await service._resolve_ticket(
            reading(daily_number=leaf(41, confidence=0.7, raw="4l"))
        )

        assert resolved.daily_number.value == 41
        assert resolved.daily_number.raw_text == "4l"
        assert resolved.daily_number.needs_review is True


# ------------------------------------------------------------- la llamada entera


class TestTheLookupCall:
    @staticmethod
    def _payload(**header: Any) -> dict[str, Any]:
        return {
            "header": {
                "date": {"day": leaf(8), "month": leaf(8), "year": leaf(2026)},
                **header,
            },
            "customer": {},
            "garments": [],
            "services": {},
        }

    async def test_it_answers_the_ticket_and_records_the_reading(self) -> None:
        order = FakeOrder(daily_number=41)
        extractor = StubExtractor(self._payload(daily_number=leaf(41)))
        service, scans, _ = build([order], extractor)

        answer = await service.lookup(JPEG, actor=_actor())

        assert answer.status is ScanStatus.COMPLETED
        assert [match.order_id for match in answer.matches] == [order.id]
        assert answer.latency_ms == 812
        job = scans.jobs[0]
        assert job.extracted["matched"] == [str(order.id)]

    async def test_a_lookup_is_not_a_capture(self) -> None:
        """They share the prompt, the provider and the budget — not the metric
        of §9, which is only meaningful over drafts somebody corrected."""
        extractor = StubExtractor(self._payload(daily_number=leaf(41)))
        service, scans, _ = build([FakeOrder(daily_number=41)], extractor)

        await service.lookup(JPEG, actor=_actor())

        assert scans.jobs[0].purpose is ScanPurpose.LOOKUP
        assert scans.jobs[0].corrections is None

    async def test_the_photo_is_not_kept(self) -> None:
        """There is nothing to review side by side: the person is holding the
        original, and the picture carries a name, a phone and a NIT."""
        extractor = StubExtractor(self._payload(daily_number=leaf(41)))
        service, scans, _ = build([FakeOrder(daily_number=41)], extractor)

        await service.lookup(JPEG, actor=_actor())

        assert scans.jobs[0].image_path == ""

    async def test_a_provider_that_is_down_leaves_the_evidence(self) -> None:
        """The row is kept, not rolled back: it is what tells "the provider is
        down" apart from "nobody is scanning"."""
        service, scans, _ = build([], StubExtractor(fails=True))

        with pytest.raises(ScanFailure):
            await service.lookup(JPEG, actor=_actor())

        assert scans.jobs[0].status is ScanStatus.FAILED
        assert scans.jobs[0].purpose is ScanPurpose.LOOKUP
        assert "could not be reached" in (scans.jobs[0].error or "")

    async def test_the_daily_budget_is_shared_with_capture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The budget belongs to the shop, not to which button was pressed."""
        from src.core.config import get_settings

        monkeypatch.setattr(get_settings(), "scan_daily_limit", 50, raising=False)
        service, scans, _ = build([], StubExtractor(self._payload()))
        scans.today_count = 50

        with pytest.raises(ScanFailure, match="daily scan limit"):
            await service.lookup(JPEG, actor=_actor())

    async def test_switched_off_means_switched_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.core.config import get_settings

        monkeypatch.setattr(get_settings(), "scan_enabled", False, raising=False)
        service, _, _ = build([])

        with pytest.raises(ScanFailure, match="switched off"):
            await service.lookup(JPEG, actor=_actor())


def _actor() -> Any:
    """Only `id` is read, and only to stamp the row."""

    @dataclass
    class Actor:
        id: UUID = field(default_factory=uuid4)

    return Actor()
