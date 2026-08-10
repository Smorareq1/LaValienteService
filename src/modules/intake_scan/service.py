"""Use cases of the scan module (Plan 0003 §4).

The order of events is the whole design: photograph → provider → **our**
deterministic validation → **our** pricing engine → a draft a person confirms.
The model never writes an order; by the time anything is saved, a human has
looked at every field it was unsure about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from src.core.business_time import business_date
from src.core.config import get_settings
from src.core.exceptions import ConflictError, NotFoundError
from src.modules.catalog.repository import CatalogRepository
from src.modules.customers.models import Customer
from src.modules.customers.repository import CustomersRepository
from src.modules.identity.models import User
from src.modules.intake_scan import storage
from src.modules.intake_scan.extractor import ScanExtractor, ScanFailure
from src.modules.intake_scan.matching import best_match, phone_for_lookup
from src.modules.intake_scan.models import ScanJob, ScanPurpose, ScanStatus
from src.modules.intake_scan.normalization import (
    Normalized,
    check_total,
    draft_charges,
    draft_field,
    normalize,
    normalize_serial,
    resolve_date,
)
from src.modules.intake_scan.repository import ScanRepository
from src.modules.intake_scan.schemas import (
    MATCHED_ON_BOOKLET_SERIAL,
    MATCHED_ON_DAILY_NUMBER,
    CustomerMatch,
    DraftField,
    RawScan,
    ScanDraft,
    ScanLookupMatch,
    ScanLookupRead,
    ScanRead,
)
from src.modules.orders.pricing import PriceBook, PricedOrder, price_order
from src.modules.orders.repository import OrdersRepository
from src.modules.orders.schemas import OrderCreate

logger = logging.getLogger(__name__)

#: How many name candidates the fuzzy match looks at. The shop has hundreds of
#: customers, not millions, and the query is already narrowed by a name fragment.
NAME_CANDIDATES = 25


@dataclass
class ResolvedTicket:
    """What a delivery lookup made of one reading.

    Both halves travel together because the screen needs both: the tickets it
    found, and what the model thought it was reading when it found them. A match
    the counter disagrees with is only arguable if the reading is visible.
    """

    order_date: DraftField[date]
    daily_number: DraftField[int]
    booklet_serial: DraftField[str]
    matches: list[ScanLookupMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ScanService:
    """Reads a ticket and hands back a draft. Never saves one."""

    def __init__(
        self,
        repository: ScanRepository,
        extractor: ScanExtractor,
        catalog: CatalogRepository,
        customers: CustomersRepository,
        orders: OrdersRepository,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.catalog = catalog
        self.customers = customers
        #: Read-only, and only for the delivery lookup: this module identifies
        #: tickets, it never writes one (D2).
        self.orders = orders

    async def scan(self, image: bytes, *, actor: User) -> ScanRead:
        """The whole of §4 for one photograph."""
        settings = get_settings()
        if not settings.scan_enabled:
            raise ScanFailure("Scanning is switched off on this deployment.")
        await self._check_daily_limit()

        scan_id = uuid4()
        image_path = await storage.store(scan_id, image)

        job = ScanJob(
            id=scan_id,
            status=ScanStatus.PROCESSING,
            image_path=image_path,
            model=settings.scan_model,
            prompt_version=settings.scan_prompt_version,
            created_by_id=actor.id,
        )
        self.repository.add(job)
        await self.repository.flush()

        try:
            extraction = await self.extractor.extract(image)
        except ScanFailure as failure:
            # The row is kept, not rolled back: a failed reading is evidence. It
            # is what tells "the provider is down" apart from "nobody is
            # scanning", and it is the first thing to look at when the counter
            # says the button stopped working.
            job.status = ScanStatus.FAILED
            job.error = str(failure)
            await self.repository.commit()
            logger.warning(
                "scan failed", extra={"scan_id": str(scan_id), "error": str(failure)}
            )
            raise

        job.model = extraction.model
        job.prompt_version = extraction.prompt_version
        job.latency_ms = extraction.latency_ms
        job.raw_response = extraction.payload

        raw = RawScan.model_validate(extraction.payload)
        draft, warnings = await self.build_draft(raw)

        job.status = ScanStatus.COMPLETED
        job.extracted = draft.model_dump(mode="json")
        job.warnings = warnings
        await self.repository.commit()

        logger.info(
            "scan completed",
            extra={
                "scan_id": str(scan_id),
                "model": extraction.model,
                "prompt_version": extraction.prompt_version,
                "latency_ms": extraction.latency_ms,
                "warnings": len(warnings),
            },
        )
        return self._view(job, draft)

    async def lookup(self, image: bytes, *, actor: User) -> ScanLookupRead:
        """Find the ticket in somebody's hand (Plan 0006 §7.1.1).

        The same photograph, the same provider and the same prompt as `scan` —
        only the question is different. There, the paper is about to become a
        ticket; here it already is one, and all that is wanted of the reading is
        the two identifiers that name it. Everything after the extraction is
        therefore skipped: no pricing, no customer match, no draft.

        Sharing the prompt rather than writing a lean one for the header is
        deliberate (D5, D6). A second prompt would be a second thing to version,
        to keep in step with the paper when the print shop changes the form, and
        to regress separately — for a saving of a few cents on a call that is
        made a few dozen times a day.

        The photo is **not** kept. `scan` stores it because §4 puts it beside the
        draft for review; here there is nothing to review — the person is holding
        the original.
        """
        settings = get_settings()
        if not settings.scan_enabled:
            raise ScanFailure("Scanning is switched off on this deployment.")
        await self._check_daily_limit()

        job = ScanJob(
            id=uuid4(),
            status=ScanStatus.PROCESSING,
            purpose=ScanPurpose.LOOKUP,
            image_path="",
            model=settings.scan_model,
            prompt_version=settings.scan_prompt_version,
            created_by_id=actor.id,
        )
        self.repository.add(job)
        await self.repository.flush()

        try:
            extraction = await self.extractor.extract(image)
        except ScanFailure as failure:
            job.status = ScanStatus.FAILED
            job.error = str(failure)
            await self.repository.commit()
            logger.warning(
                "scan lookup failed",
                extra={"scan_id": str(job.id), "error": str(failure)},
            )
            raise

        job.model = extraction.model
        job.prompt_version = extraction.prompt_version
        job.latency_ms = extraction.latency_ms
        job.raw_response = extraction.payload

        raw = RawScan.model_validate(extraction.payload)
        resolved = await self._resolve_ticket(raw)

        job.status = ScanStatus.COMPLETED
        job.warnings = resolved.warnings
        # What it read and what that turned out to be, so a complaint about the
        # wrong ticket coming up can be answered from the row.
        job.extracted = {
            "order_date": resolved.order_date.model_dump(mode="json"),
            "daily_number": resolved.daily_number.model_dump(mode="json"),
            "booklet_serial": resolved.booklet_serial.model_dump(mode="json"),
            "matched": [str(match.order_id) for match in resolved.matches],
        }
        await self.repository.commit()

        logger.info(
            "scan lookup completed",
            extra={
                "scan_id": str(job.id),
                "latency_ms": extraction.latency_ms,
                "matches": len(resolved.matches),
                "warnings": len(resolved.warnings),
            },
        )
        return ScanLookupRead(
            id=job.id,
            status=job.status,
            model=job.model,
            prompt_version=job.prompt_version,
            latency_ms=job.latency_ms,
            warnings=resolved.warnings,
            order_date=resolved.order_date,
            daily_number=resolved.daily_number,
            booklet_serial=resolved.booklet_serial,
            matches=resolved.matches,
        )

    async def _resolve_ticket(self, raw: RawScan) -> ResolvedTicket:
        """From a reading to the ticket it names.

        Legibility is the gate, not confidence alone: a serial the model is 30%
        sure about is a guess, and a guess that resolves to a real ticket is
        worse than no answer, because it hands back somebody else's clothes.
        """
        today = business_date()
        order_date, date_warnings = resolve_date(raw, today)
        daily_number = draft_field(
            raw.header.daily_number.value,
            raw.header.daily_number.confidence,
            raw.header.daily_number.raw_text,
        )
        serial_value = normalize_serial(raw.header.booklet_serial.value)
        booklet_serial = draft_field(
            serial_value,
            raw.header.booklet_serial.confidence,
            raw.header.booklet_serial.raw_text,
        )

        wanted_serial = serial_value if raw.header.booklet_serial.is_legible else None
        wanted_number = (
            raw.header.daily_number.value if raw.header.daily_number.is_legible else None
        )
        if wanted_serial is None and wanted_number is None:
            return ResolvedTicket(
                order_date=order_date,
                daily_number=daily_number,
                booklet_serial=booklet_serial,
                matches=[],
                warnings=["ticket_unreadable"],
            )

        # `resolve_date` answers "today" for a blank box, which is right for a
        # capture and right here too: the tickets being handed back at closing
        # time are overwhelmingly the ones taken that morning.
        orders = await self.orders.find_by_ticket(
            booklet_serial=wanted_serial,
            order_date=order_date.value,
            daily_number=wanted_number,
        )

        warnings = list(date_warnings)
        matches = [
            ScanLookupMatch(
                order_id=order.id,
                order_date=order.order_date,
                daily_number=order.daily_number,
                booklet_serial=order.booklet_serial,
                customer_id=order.customer_id,
                status=order.status,
                total_pieces=order.total_pieces,
                total=order.total,
                paid_total=order.paid_total,
                balance=order.balance,
                matched_on=(
                    MATCHED_ON_BOOKLET_SERIAL
                    if wanted_serial is not None and order.booklet_serial == wanted_serial
                    else MATCHED_ON_DAILY_NUMBER
                ),
            )
            for order in orders
        ]

        if not matches:
            # Named so the screen can say which number it looked for. A ticket
            # that is not here is usually a misread digit, and showing the digit
            # is what lets somebody correct it by typing.
            wanted = wanted_serial or str(wanted_number)
            warnings.append(f"no_match:{wanted}")
        elif len(matches) > 1:
            warnings.append("serial_and_number_disagree")

        return ResolvedTicket(
            order_date=order_date,
            daily_number=daily_number,
            booklet_serial=booklet_serial,
            matches=matches,
            warnings=warnings,
        )

    async def get(self, scan_id: UUID) -> ScanRead:
        job = await self.repository.get(scan_id)
        if job is None:
            raise NotFoundError("Scan not found.")
        draft = ScanDraft.model_validate(job.extracted) if job.extracted else None
        return self._view(job, draft)

    async def get_image(self, scan_id: UUID) -> tuple[bytes, str]:
        """The original photograph, for the side-by-side review of §4."""
        job = await self.repository.get(scan_id)
        if job is None:
            raise NotFoundError("Scan not found.")
        content = await storage.read(job.image_path)
        if content is None:
            # Most likely the retention script of D9 already took it, which is
            # working as intended and worth saying plainly.
            raise NotFoundError("That scan's photo is no longer stored.")
        suffix = job.image_path.rsplit(".", 1)[-1].lower()
        return content, {"png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")

    async def build_draft(self, raw: RawScan) -> tuple[ScanDraft, list[str]]:
        """§7 end to end: normalize, match the customer, reprice, compare."""
        today = business_date()
        garment_types = await self.catalog.list_garment_types()
        normalized = normalize(raw, today=today, garment_types=garment_types)

        priced, pricing_warnings = await self._reprice(normalized)
        warnings = [*normalized.warnings, *pricing_warnings]

        computed_total = priced.total if priced else Decimal(0)
        warnings.extend(check_total(normalized.total_read, computed_total))

        match = await self._match_customer(normalized)

        priced_by_index: dict[int, tuple[str, Decimal]] = {}
        if priced is not None:
            for index, line in enumerate(priced.charges):
                priced_by_index[index] = (line.description, line.amount)

        draft = ScanDraft(
            order_date=normalized.order_date,
            daily_number=normalized.daily_number,
            booklet_serial=normalized.booklet_serial,
            nit=normalized.nit,
            weight_lbs=normalized.weight_lbs,
            observations=normalized.observations,
            customer_name=normalized.customer_name,
            customer_phone=normalized.customer_phone,
            customer_address=normalized.customer_address,
            customer_match=match,
            garments=normalized.garments,
            charges=draft_charges(
                normalized.charges, normalized.charge_confidence, priced_by_index
            ),
            estimated_subtotal=priced.subtotal if priced else Decimal(0),
            estimated_total=computed_total,
            total_read=normalized.total_read,
        )
        return draft, warnings

    async def _reprice(
        self, normalized: Normalized
    ) -> tuple[PricedOrder | None, list[str]]:
        """Run the engine of Plan 0001 §6 over what was read.

        Wrapped rather than let to raise: the engine refuses a ticket it cannot
        price, which is right when someone is capturing and wrong here — a
        service the catalog does not know means the *reading* was off, and the
        person still needs the rest of the draft to correct it by hand.
        """
        if not normalized.charges:
            return None, []

        order_date = normalized.order_date.value or business_date()
        service_types = await self.catalog.list_service_types()
        prices = await self.catalog.list_prices_on(order_date)
        book = PriceBook(service_types, prices, order_date)
        pieces = sum(garment.quantity for garment in normalized.garments)

        try:
            priced = price_order(
                normalized.charges,
                [],
                book,
                weight_lbs=normalized.weight_lbs.value,
                total_pieces=pieces,
            )
        except ConflictError as error:
            return None, [f"pricing_failed:{error}"]

        # The engine's own warnings (a hand-wash level outside its piece range)
        # ride along coded, like everything else the screen has to phrase.
        return priced, [f"pricing:{warning}" for warning in priced.warnings]

    async def _match_customer(self, normalized: Normalized) -> CustomerMatch | None:
        phone = phone_for_lookup(normalized.customer_phone.value)
        by_phone = await self.customers.find_by_phone(phone) if phone else None

        candidates: list[Customer] = []
        name = normalized.customer_name.value
        if by_phone is None and name:
            # Narrowed by the longest word of the name, which is the one least
            # likely to be a common surname and the cheapest filter available
            # before the fuzzy comparison happens in Python.
            longest = max(name.split(), key=len, default="")
            if len(longest) >= 3:
                candidates, _ = await self.customers.search(
                    search=longest, page_size=NAME_CANDIDATES
                )

        match = best_match(
            phone=phone, full_name=name, by_phone=by_phone, candidates=candidates
        )
        if match is None:
            return None
        return CustomerMatch(
            customer_id=match.customer.id,
            full_name=match.customer.full_name,
            phone=match.customer.phone,
            score=match.score,
            matched_on=match.matched_on,
        )

    async def link_order(self, scan_id: UUID, order: Any, data: OrderCreate) -> None:
        """Close the loop (D7): the ticket that came out of this reading.

        The diff is the quality metric of §9 — the only one measured on real
        tickets rather than on the golden set. It is written best-effort: a scan
        that cannot be linked must never take an order down with it, because the
        order is the thing that matters and it is already saved.
        """
        job = await self.repository.get(scan_id)
        if job is None or job.extracted is None:
            return
        proposed = ScanDraft.model_validate(job.extracted)
        job.order_id = order.id
        job.corrections = _diff(proposed, data, order).model_dump(mode="json")
        await self.repository.commit()

    async def _check_daily_limit(self) -> None:
        limit = get_settings().scan_daily_limit
        if limit and await self.repository.count_today() >= limit:
            raise ScanFailure(
                f"The daily scan limit of {limit} has been reached. "
                "Capture by hand today, or raise SCAN_DAILY_LIMIT."
            )

    @staticmethod
    def _view(job: ScanJob, draft: ScanDraft | None) -> ScanRead:
        return ScanRead(
            id=job.id,
            status=job.status,
            model=job.model,
            prompt_version=job.prompt_version,
            latency_ms=job.latency_ms,
            order_id=job.order_id,
            error=job.error,
            warnings=list(job.warnings or []),
            draft=draft,
        )


def _diff(proposed: ScanDraft, saved: OrderCreate, order: Any) -> Any:
    """Field-by-field, what the person changed (D7).

    Only fields the model actually proposed are counted. Scoring it against
    everything on the form would drown the signal: a scan that read nine of ten
    fields perfectly and left the tenth blank did not get 10% wrong.
    """
    from src.modules.intake_scan.schemas import ScanCorrections

    fields: dict[str, Any] = {}

    def compare(name: str, proposed_value: Any, saved_value: Any) -> None:
        if proposed_value is None:
            return
        if _same(proposed_value, saved_value):
            return
        fields[name] = {
            "proposed": _plain(proposed_value),
            "saved": _plain(saved_value),
        }

    compare("booklet_serial", proposed.booklet_serial.value, saved.booklet_serial)
    compare("nit", proposed.nit.value, saved.nit)
    compare("weight_lbs", proposed.weight_lbs.value, saved.weight_lbs)
    compare("observations", proposed.observations.value, saved.observations)
    compare(
        "order_date",
        proposed.order_date.value,
        saved.order_date or getattr(order, "order_date", None),
    )

    proposed_garments = {str(line.garment_type_id): line.quantity for line in proposed.garments}
    saved_garments = {str(line.garment_type_id): line.quantity for line in saved.garments}
    if proposed_garments and proposed_garments != saved_garments:
        fields["garments"] = {"proposed": proposed_garments, "saved": saved_garments}

    proposed_charges = sorted(
        (line.service_code, line.option_code, str(line.quantity)) for line in proposed.charges
    )
    saved_charges = sorted(
        (line.service_code, line.option_code, str(line.quantity)) for line in saved.charges
    )
    if proposed_charges and proposed_charges != saved_charges:
        fields["charges"] = {
            "proposed": [list(line) for line in proposed_charges],
            "saved": [list(line) for line in saved_charges],
        }

    total_proposed = _proposed_count(proposed)
    return ScanCorrections(
        fields=fields, corrected=len(fields), proposed=total_proposed
    )


def _proposed_count(proposed: ScanDraft) -> int:
    """How many things the model actually offered an answer for."""
    singles = [
        proposed.booklet_serial.value,
        proposed.nit.value,
        proposed.weight_lbs.value,
        proposed.observations.value,
        proposed.order_date.value,
    ]
    count = sum(1 for value in singles if value is not None)
    if proposed.garments:
        count += 1
    if proposed.charges:
        count += 1
    return count


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except Exception:
            return False
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    return bool(left == right)


def _plain(value: Any) -> Any:
    """JSONB-safe: dates and decimals become strings, everything else stays."""
    if isinstance(value, Decimal | date | datetime):
        return str(value)
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)
