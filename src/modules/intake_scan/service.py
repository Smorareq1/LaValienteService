"""Use cases of the scan module (Plan 0003 §4).

The order of events is the whole design: photograph → provider → **our**
deterministic validation → **our** pricing engine → a draft a person confirms.
The model never writes an order; by the time anything is saved, a human has
looked at every field it was unsure about.
"""

from __future__ import annotations

import logging
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
from src.modules.intake_scan.models import ScanJob, ScanStatus
from src.modules.intake_scan.normalization import (
    Normalized,
    check_total,
    draft_charges,
    normalize,
)
from src.modules.intake_scan.repository import ScanRepository
from src.modules.intake_scan.schemas import (
    CustomerMatch,
    RawScan,
    ScanDraft,
    ScanRead,
)
from src.modules.orders.pricing import PriceBook, PricedOrder, price_order
from src.modules.orders.schemas import OrderCreate

logger = logging.getLogger(__name__)

#: How many name candidates the fuzzy match looks at. The shop has hundreds of
#: customers, not millions, and the query is already narrowed by a name fragment.
NAME_CANDIDATES = 25


class ScanService:
    """Reads a ticket and hands back a draft. Never saves one."""

    def __init__(
        self,
        repository: ScanRepository,
        extractor: ScanExtractor,
        catalog: CatalogRepository,
        customers: CustomersRepository,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        self.catalog = catalog
        self.customers = customers

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
