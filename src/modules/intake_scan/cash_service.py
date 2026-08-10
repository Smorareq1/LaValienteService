"""Importing a day off the «Registro Diario» sheet (Plan 0005 §1, Plan 0003 §4).

Two use cases, and everything about the design is in the gap between them.

`scan` **reads**: a photograph goes to the provider, comes back as rows, and each
row is matched against the database — the ticket a `#Tomapedido` names, the
balance it owes, the category those words mean, the employee whose hours those
are. Nothing is written. What comes back is a proposal.

`apply` **writes**, and it is the only place in this module that does. That is a
real departure from D2, which kept the scan module read-only so that deleting it
the day the paper booklet goes would touch nothing else — so it is worth being
precise about how it is kept true:

* every write goes through the owning module's **service**, never its tables.
  `OrdersService.add_payment` still clamps to the balance, `deliver` still asks
  for `orders.deliver_unpaid` when money is left standing, `ExpensesService`
  still refuses a closed day. The importer cannot do anything a person with the
  same permissions could not do by hand, one screen at a time.
* nothing arrives from the reading. `apply` takes ids and amounts a **person**
  confirmed on screen (`CashSheetApply`), and the draft's confidences never reach
  it. The photograph proposes; the person disposes.
* the direction of the dependency is unchanged: this file imports the other
  modules, and none of them import it.

The failure mode it is built around is a counter with fifteen rows to import and
one of them wrong. A single transaction would lose the fourteen good ones to the
bad one, and the person would have to work out by hand which had gone through. So
each row is its own attempt, each outcome is reported by index, and the answer
says exactly what happened to every line.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.core.business_time import business_date
from src.core.config import get_settings
from src.core.exceptions import ConflictError, NotFoundError
from src.modules.daily_close.repository import DailyCloseRepository
from src.modules.expenses.schemas import ExpenseCreate
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.intake_scan import storage
from src.modules.intake_scan.cash_normalization import (
    attendance_row,
    check_sum,
    expense_row,
    income_row,
    resolve_sheet_date,
    settle_income,
    sheet_warnings,
)
from src.modules.intake_scan.cash_schemas import (
    INCOME_SUPPLY,
    CashAppliedRow,
    CashAttendanceApply,
    CashExpenseApply,
    CashIncomeApply,
    CashSheetApply,
    CashSheetApplyResult,
    CashSheetDay,
    CashSheetDraft,
    CashSheetRead,
    RawCashSheet,
)
from src.modules.intake_scan.extractor import ScanDocument, ScanExtractor, ScanFailure
from src.modules.intake_scan.models import ScanJob, ScanPurpose, ScanStatus
from src.modules.intake_scan.normalization import draft_field
from src.modules.intake_scan.repository import ScanRepository
from src.modules.orders.repository import OrdersRepository, serial_key
from src.modules.orders.schemas import OrderDeliver, OrderPaymentCreate
from src.modules.orders.service import OrdersService
from src.modules.staff.schemas import AttendanceCreate
from src.modules.staff.service import StaffService

logger = logging.getLogger(__name__)

#: Namespace for the ids this importer mints. Deriving them from the scan and the
#: row — instead of drawing a fresh uuid4 — is what makes a retried request safe:
#: the second attempt asks to create rows that already exist, and both
#: `OrdersService._take_payment` and `ExpensesService.create_expense` already
#: answer "that one is already registered" to their own id. A dropped connection
#: therefore costs a repeat, not a double charge.
ROW_NAMESPACE = uuid5(NAMESPACE_URL, "https://lavalientecoban.gt/scans/cash-close")


def row_id(scan_id: UUID, kind: str, index: int) -> UUID:
    """The id row `index` of `kind` will always be written under."""
    return uuid5(ROW_NAMESPACE, f"{scan_id}:{kind}:{index}")


class CashSheetService:
    """Reads the daily sheet, and — once a person has confirmed it — files it."""

    def __init__(
        self,
        repository: ScanRepository,
        extractor: ScanExtractor,
        orders_repository: OrdersRepository,
        orders: OrdersService,
        expenses: ExpensesService,
        staff: StaffService,
        closed_days: DailyCloseRepository | None = None,
    ) -> None:
        self.repository = repository
        self.extractor = extractor
        #: Read-only, for the matching: which ticket a booklet number names.
        self.orders_repository = orders_repository
        #: The three services that own what an import writes. Held as services and
        #: not as repositories on purpose — see the module docstring.
        self.orders = orders
        self.expenses = expenses
        self.staff = staff
        self.closed_days = closed_days

    # ------------------------------------------------------------------ leer

    async def scan(self, image: bytes, *, actor: User) -> CashSheetRead:
        """Read a sheet and propose what to do with it. Writes nothing."""
        settings = get_settings()
        if not settings.scan_enabled:
            raise ScanFailure("Scanning is switched off on this deployment.")
        await self._check_daily_limit()

        scan_id = uuid4()
        # Kept, unlike a lookup's: the review screen puts the rows beside the
        # photograph, and an import of somebody's money is exactly the operation
        # worth being able to look back at (D9 still expires it on schedule).
        image_path = await storage.store(scan_id, image)

        job = ScanJob(
            id=scan_id,
            status=ScanStatus.PROCESSING,
            purpose=ScanPurpose.CASH_CLOSE,
            image_path=image_path,
            model=settings.scan_model,
            prompt_version=settings.scan_cash_prompt_version,
            created_by_id=actor.id,
        )
        self.repository.add(job)
        await self.repository.flush()

        try:
            extraction = await self.extractor.extract(
                image, document=ScanDocument.CASH_SHEET
            )
        except ScanFailure as failure:
            job.status = ScanStatus.FAILED
            job.error = str(failure)
            await self.repository.commit()
            logger.warning(
                "cash sheet scan failed",
                extra={"scan_id": str(scan_id), "error": str(failure)},
            )
            raise

        job.model = extraction.model
        job.prompt_version = extraction.prompt_version
        job.latency_ms = extraction.latency_ms
        job.raw_response = extraction.payload

        raw = RawCashSheet.model_validate(extraction.payload)
        draft, warnings = await self.build_draft(raw)

        job.status = ScanStatus.COMPLETED
        job.extracted = draft.model_dump(mode="json")
        job.warnings = warnings
        await self.repository.commit()

        logger.info(
            "cash sheet scan completed",
            extra={
                "scan_id": str(scan_id),
                "latency_ms": extraction.latency_ms,
                "days": len(draft.days),
                "warnings": len(warnings),
            },
        )
        return self._view(job, draft)

    async def build_draft(self, raw: RawCashSheet) -> tuple[CashSheetDraft, list[str]]:
        """Every row of every block, matched against the database.

        The queries are done once for the whole sheet rather than once per row:
        the categories and the staff are small enough to hold, and the tickets are
        fetched by the whole column of serials at a time.
        """
        today = business_date()
        categories = await self.expenses.list_categories()
        employees = await self.staff.list_employees()

        serials = [
            serial
            for block in raw.blocks
            for row in block.incomes
            if (serial := serial_key(row.booklet_serial.value)) is not None
        ]
        by_serial = await self.orders_repository.find_by_serials(serials)

        days: list[CashSheetDay] = []
        for block in raw.blocks:
            close_date, confidence, warnings = resolve_sheet_date(
                block.date.day.value,
                block.date.month.value,
                block.date.year.value,
                today=today,
            )

            incomes = []
            for index, raw_income in enumerate(block.incomes):
                row = income_row(index, raw_income)
                candidates = by_serial.get(serial_key(row.booklet_serial.value) or "", [])
                if len(candidates) > 1:
                    # Two live tickets under one printed serial. Said out loud
                    # rather than resolved: whichever is picked would be a guess
                    # about whose money this is.
                    warnings.append(f"duplicate_serial:{row.booklet_serial.value}")
                incomes.append(settle_income(row, candidates[0] if candidates else None))

            expenses = [
                expense_row(index, raw_expense, categories=categories, employees=employees)
                for index, raw_expense in enumerate(block.expenses)
            ]
            attendance = [
                attendance_row(index, raw_line, employees=employees)
                for index, raw_line in enumerate(block.attendance)
            ]

            # The rows as *read*, which is what the written column total can be
            # compared against. The suggested amounts are a different number —
            # they are bounded by what each ticket owes — and comparing those
            # against the paper would flag every ticket with an advance on it.
            income_rows_total = sum(
                (
                    row.amount_read.value or Decimal(0)
                    for row in incomes
                    if row.status != INCOME_SUPPLY
                ),
                Decimal(0),
            )
            expense_rows_total = sum(
                (row.amount.value or Decimal(0) for row in expenses), Decimal(0)
            )

            warnings.extend(
                check_sum(
                    _read(block.totals.income_total.value),
                    income_rows_total,
                    "income_sum_mismatch",
                )
            )
            warnings.extend(
                check_sum(
                    _read(block.totals.expenses_total.value),
                    expense_rows_total,
                    "expense_sum_mismatch",
                )
            )
            if self.closed_days is not None and await self.closed_days.is_closed(close_date):
                warnings.append(f"day_closed:{close_date.isoformat()}")

            days.append(
                CashSheetDay(
                    close_date=draft_field(close_date, confidence),
                    incomes=incomes,
                    expenses=expenses,
                    attendance=attendance,
                    income_total_read=_read(block.totals.income_total.value),
                    expenses_total_read=_read(block.totals.expenses_total.value),
                    accumulated_read=_read(block.totals.accumulated.value),
                    income_total_rows=income_rows_total,
                    expenses_total_rows=expense_rows_total,
                    warnings=warnings,
                )
            )

        return CashSheetDraft(days=days), sheet_warnings(raw)

    async def get(self, scan_id: UUID) -> CashSheetRead:
        job = await self._require(scan_id)
        draft = CashSheetDraft.model_validate(job.extracted) if job.extracted else None
        return self._view(job, draft)

    # --------------------------------------------------------------- aplicar

    async def apply(
        self, scan_id: UUID, data: CashSheetApply, *, actor: User
    ) -> CashSheetApplyResult:
        """Write down what the person confirmed (§7).

        Refuses a sheet that was already imported. That is not the same guard as
        the derived ids above — those make a *retry* harmless — and it exists for
        the different mistake of somebody photographing yesterday's sheet again
        tonight: every row would look collectable a second time only because the
        first import moved the balances, and the ones that did not would come
        back as errors nobody can interpret.
        """
        job = await self._require(scan_id)
        if job.purpose is not ScanPurpose.CASH_CLOSE:
            raise ConflictError("That scan is not a daily sheet.")
        if job.applied_at is not None:
            raise ConflictError(
                f"That sheet was already imported on "
                f"{job.applied_at.isoformat(timespec='minutes')}."
            )

        result = CashSheetApplyResult(close_date=data.close_date)

        for row in data.incomes:
            outcome = await self._collect(scan_id, row, actor=actor)
            result.incomes.append(outcome)
            if outcome.outcome == "applied":
                result.payments_applied += 1
                result.collected_total += row.amount
                if row.deliver:
                    result.orders_delivered += 1

        for expense in data.expenses:
            outcome = await self._spend(scan_id, expense, data.close_date, actor=actor)
            result.expenses.append(outcome)
            if outcome.outcome == "applied":
                result.expenses_created += 1
                result.expenses_total += expense.amount

        for record in data.attendance:
            outcome = await self._clock(scan_id, record, data.close_date)
            result.attendance.append(outcome)
            if outcome.outcome == "applied":
                result.attendance_created += 1

        job.applied_at = datetime.now(UTC)
        job.applied_result = result.model_dump(mode="json")
        await self.repository.commit()

        logger.info(
            "cash sheet applied",
            extra={
                "scan_id": str(scan_id),
                "close_date": data.close_date.isoformat(),
                "payments": result.payments_applied,
                "expenses": result.expenses_created,
                "collected": str(result.collected_total),
            },
        )
        return result

    async def _collect(
        self, scan_id: UUID, row: CashIncomeApply, *, actor: User
    ) -> CashAppliedRow:
        """One ticket: take the money, and hand the clothes back if asked.

        The payment and the delivery are one call whenever both are wanted, which
        is what `OrderDeliver.payment` is for: it is a single act at the counter,
        and splitting it would leave a ticket paid but undelivered if the second
        call failed.
        """
        payment = OrderPaymentCreate(
            amount=row.amount,
            method=row.method,
            id=row_id(scan_id, "payment", row.index),
        )
        try:
            if row.deliver:
                await self.orders.deliver(
                    row.order_id, OrderDeliver(payment=payment), actor=actor
                )
            else:
                await self.orders.add_payment(row.order_id, payment, actor=actor)
        except ConflictError as error:
            outcome = "skipped" if "already registered" in str(error).lower() else "failed"
            return CashAppliedRow(index=row.index, outcome=outcome, reason=str(error))
        except NotFoundError as error:
            return CashAppliedRow(index=row.index, outcome="failed", reason=str(error))
        return CashAppliedRow(index=row.index, outcome="applied")

    async def _spend(
        self, scan_id: UUID, row: CashExpenseApply, close_date: date, *, actor: User
    ) -> CashAppliedRow:
        try:
            await self.expenses.create_expense(
                ExpenseCreate(
                    id=row_id(scan_id, "expense", row.index),
                    expense_date=close_date,
                    category_id=row.category_id,
                    concept=row.concept,
                    amount=row.amount,
                    method=row.method,
                    status=row.status,
                    employee_id=row.employee_id,
                    observations=row.observations,
                ),
                actor=actor,
            )
        except ConflictError as error:
            # "Already registered" is the retry landing on its own first attempt,
            # which is the derived id doing its job: reported as skipped, not as
            # a failure somebody has to investigate.
            outcome = "skipped" if "already registered" in str(error).lower() else "failed"
            return CashAppliedRow(index=row.index, outcome=outcome, reason=str(error))
        except NotFoundError as error:
            return CashAppliedRow(index=row.index, outcome="failed", reason=str(error))
        return CashAppliedRow(index=row.index, outcome="applied")

    async def _clock(
        self, scan_id: UUID, row: CashAttendanceApply, close_date: date
    ) -> CashAppliedRow:
        try:
            await self.staff.clock_in(
                AttendanceCreate(
                    id=row_id(scan_id, "attendance", row.index),
                    employee_id=row.employee_id,
                    work_date=close_date,
                    clock_in=row.clock_in,
                    clock_out=row.clock_out,
                    notes=row.notes,
                )
            )
        except ConflictError as error:
            message = str(error).lower()
            already = "already registered" in message or "already has an open" in message
            return CashAppliedRow(
                index=row.index,
                outcome="skipped" if already else "failed",
                reason=str(error),
            )
        except NotFoundError as error:
            return CashAppliedRow(index=row.index, outcome="failed", reason=str(error))
        return CashAppliedRow(index=row.index, outcome="applied")

    # ---------------------------------------------------------------- común

    async def _require(self, scan_id: UUID) -> ScanJob:
        job = await self.repository.get(scan_id)
        if job is None:
            raise NotFoundError("Scan not found.")
        return job

    async def _check_daily_limit(self) -> None:
        limit = get_settings().scan_daily_limit
        if limit and await self.repository.count_today() >= limit:
            raise ScanFailure(
                f"The daily scan limit of {limit} has been reached. "
                "Capture by hand today, or raise SCAN_DAILY_LIMIT."
            )

    @staticmethod
    def _view(job: ScanJob, draft: CashSheetDraft | None) -> CashSheetRead:
        warnings = list(job.warnings or [])
        if job.applied_at is not None:
            warnings.append(
                f"already_applied:{job.applied_at.isoformat(timespec='minutes')}"
            )
        return CashSheetRead(
            id=job.id,
            status=job.status,
            model=job.model,
            prompt_version=job.prompt_version,
            latency_ms=job.latency_ms,
            error=job.error,
            warnings=warnings,
            draft=draft,
        )


def _read(value: object) -> Decimal | None:
    """A cross-check figure off the paper, or nothing. Never raises."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
