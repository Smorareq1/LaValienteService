"""The «Registro Diario» sheet: what is read, what is proposed, what is applied.

Three families live here and the boundaries between them are the whole safety
argument of the feature.

* `RawCash…` — what **Gemini** returns. Every leaf carries its own `confidence`
  and the literal `raw_text`, exactly as on the ticket (Plan 0003 D3). Nothing
  here is trusted: it is a reading of somebody's handwriting about money.
* `CashSheet…` (the draft) — what **we** hand the app after matching each row
  against the database: the ticket a `#Tomapedido` turns out to name, the balance
  actually owed, the expense category the words point at, the employee whose
  hours those are. Every row carries a coded `status` saying what the server
  found, and no row is ever "ready" on its own.
* `…Apply` — what the **person** confirmed, sent back to be written. This family
  deliberately shares nothing with the draft: it names ids and amounts and
  carries no confidence at all, because by the time it is sent nobody is
  guessing any more. A field that only exists to be looked at has no business
  reaching a payment.

The direction of that last step is the point. The reading proposes; a human
disposes; only then does money move.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.modules.expenses.models import ExpenseStatus
from src.modules.intake_scan.models import ScanStatus
from src.modules.intake_scan.schemas import DraftField, FieldRead, RawScanDate
from src.modules.orders.models import OrderStatus, PaymentMethod

# --------------------------------------------------------------------- lectura


class RawCashIncome(BaseModel):
    """One row of the left half: a ticket that was collected."""

    booklet_serial: FieldRead[str] = Field(default_factory=FieldRead[str])
    customer_text: FieldRead[str] = Field(default_factory=FieldRead[str])
    amount: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    #: The highlighter, which is what tells cash from transfer and a laundry
    #: ticket from a bottle of detergent sold over the counter.
    mark: FieldRead[str] = Field(default_factory=FieldRead[str])


class RawCashExpense(BaseModel):
    """One row of the right half: money that went out.

    The column names on the printed sheet lie, and Plan 0005 §1 already said so:
    `#Factura` holds the *object* paid for and `Proveedor` holds the
    *description*. The field names here are what the columns mean, not what they
    are labelled, so that nobody downstream has to remember the joke.
    """

    object_text: FieldRead[str] = Field(default_factory=FieldRead[str])
    description: FieldRead[str] = Field(default_factory=FieldRead[str])
    amount: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    observations: FieldRead[str] = Field(default_factory=FieldRead[str])


class RawCashAttendance(BaseModel):
    """The ENTRADA / ALMUERZO / SALIDA line, with whoever signed it."""

    employee_text: FieldRead[str] = Field(default_factory=FieldRead[str])
    clock_in: FieldRead[str] = Field(default_factory=FieldRead[str])
    lunch: FieldRead[str] = Field(default_factory=FieldRead[str])
    clock_out: FieldRead[str] = Field(default_factory=FieldRead[str])


class RawCashTotals(BaseModel):
    """The three sums at the foot. **Cross-check only**, like the ticket's."""

    income_total: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    expenses_total: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])
    accumulated: FieldRead[Decimal] = Field(default_factory=FieldRead[Decimal])


class RawCashBlock(BaseModel):
    """One REGISTRO DIARIO block: one day."""

    model_config = ConfigDict(extra="ignore")

    date: RawScanDate = Field(default_factory=RawScanDate)
    incomes: list[RawCashIncome] = Field(default_factory=list)
    expenses: list[RawCashExpense] = Field(default_factory=list)
    attendance: list[RawCashAttendance] = Field(default_factory=list)
    totals: RawCashTotals = Field(default_factory=RawCashTotals)


class RawCashSheet(BaseModel):
    """The whole photograph: up to three days stacked on one page."""

    model_config = ConfigDict(extra="ignore")

    blocks: list[RawCashBlock] = Field(default_factory=list)


# --------------------------------------------------------------------- borrador

#: What the server made of an income row. Coded, like every warning in this
#: codebase: the app writes the Spanish.
INCOME_MATCHED = "matched"
#: Read, but no ticket in this database carries that number.
INCOME_NOT_FOUND = "not_found"
#: The number could not be read at all, so nothing was even looked for.
INCOME_UNREADABLE = "unreadable"
#: The ticket exists and owes nothing: collected already, or delivered and paid.
INCOME_SETTLED = "settled"
#: Found, but the paper's amount and the balance owed are not the same number.
INCOME_AMOUNT_MISMATCH = "amount_mismatch"
#: Marked blue: a supply sold over the counter, not a ticket. Plan 0005 files
#: these as `supply_sales`, which needs a product and a lot — more than a name in
#: a column can settle — so the row is shown and left for the sales screen.
INCOME_SUPPLY = "supply"
#: The ticket is voided. Money against it would be money against nothing.
INCOME_CANCELLED = "cancelled"


class CashIncomeRow(BaseModel):
    """A collection the sheet claims happened, and the ticket it names.

    `amount_read` and `amount_suggested` are both here and they are not the same
    thing. The first is the paper. The second is what would actually be charged
    if the row were confirmed as it stands — the balance owed when the two agree,
    and the balance *clamped* when the paper says more, because a payment larger
    than the balance is refused by `OrdersService` and putting change in the
    books would leave the drawer short.
    """

    #: Position in the block, so the app can point at the row on the photo and so
    #: the applied payment's id can be derived from it.
    index: int
    booklet_serial: DraftField[str] = Field(default_factory=DraftField[str])
    customer_text: DraftField[str] = Field(default_factory=DraftField[str])
    amount_read: DraftField[Decimal] = Field(default_factory=DraftField[Decimal])
    #: Cash unless the row was highlighted pink.
    method: PaymentMethod = PaymentMethod.CASH
    #: `true` when the row was highlighted yellow: the customer asked for an
    #: invoice. Nothing is done with it beyond showing it — the NIT lives on the
    #: ticket, and Plan 0005 §2 keeps electronic invoicing out of phase 1.
    invoice_requested: bool = False

    status: str = INCOME_UNREADABLE
    order_id: UUID | None = None
    order_date: date | None = None
    daily_number: int | None = None
    order_status: OrderStatus | None = None
    order_total: Decimal | None = None
    balance: Decimal | None = None
    #: What confirming this row as it stands would collect. `None` when there is
    #: nothing to collect.
    amount_suggested: Decimal | None = None
    #: Whether the ticket is still in the shop, and so whether confirming should
    #: also hand the clothes back.
    can_deliver: bool = False


#: The expense row's category was matched by its words.
EXPENSE_MATCHED = "matched"
#: Nothing in the catalog of categories looks like those words. The row still
#: comes back — with its amount and its description — for a person to file.
EXPENSE_NO_CATEGORY = "no_category"
#: An amount was read but no description: not enough to write down.
EXPENSE_INCOMPLETE = "incomplete"


class CashExpenseRow(BaseModel):
    """Money that went out, ready for `ExpenseCreate` once a category is settled."""

    index: int
    concept: DraftField[str] = Field(default_factory=DraftField[str])
    amount: DraftField[Decimal] = Field(default_factory=DraftField[Decimal])
    observations: DraftField[str] = Field(default_factory=DraftField[str])

    status: str = EXPENSE_INCOMPLETE
    category_id: UUID | None = None
    category_name: str | None = None
    #: How the words were matched, for the screen to be honest about it:
    #: `exact` when the category's own name was written, `keyword` when a word of
    #: the sheet's vocabulary pointed at it.
    matched_on: str | None = None
    #: `pending` when the notes say the money has not actually left yet
    #: («pago atrasado», «de realizar»), which the sheet still counts for the day.
    expense_status: ExpenseStatus = ExpenseStatus.PAID
    method: PaymentMethod = PaymentMethod.CASH
    #: Set when the description named an employee's overtime, so the expense can
    #: be filed against the person it paid.
    employee_id: UUID | None = None
    employee_name: str | None = None


class CashAttendanceRow(BaseModel):
    """A working day as the foot of the sheet records it."""

    index: int
    employee_text: DraftField[str] = Field(default_factory=DraftField[str])
    clock_in: DraftField[time] = Field(default_factory=DraftField[time])
    clock_out: DraftField[time] = Field(default_factory=DraftField[time])

    employee_id: UUID | None = None
    employee_name: str | None = None


class CashSheetDay(BaseModel):
    """One block of the photograph, matched against the database."""

    close_date: DraftField[date] = Field(default_factory=DraftField[date])
    incomes: list[CashIncomeRow] = Field(default_factory=list)
    expenses: list[CashExpenseRow] = Field(default_factory=list)
    attendance: list[CashAttendanceRow] = Field(default_factory=list)

    #: What the paper's own sums say. Kept apart from the figures below so the
    #: screen can show both sides of a disagreement.
    income_total_read: Decimal | None = None
    expenses_total_read: Decimal | None = None
    accumulated_read: Decimal | None = None

    #: What the rows above actually add up to, row by row, as read.
    income_total_rows: Decimal = Decimal(0)
    expenses_total_rows: Decimal = Decimal(0)

    #: Coded, per day: `income_sum_mismatch:1130:1095`, `day_closed:2026-08-04`,
    #: `sheet_not_today:2026-08-04`.
    warnings: list[str] = Field(default_factory=list)


class CashSheetDraft(BaseModel):
    """The whole reading, day by day."""

    days: list[CashSheetDay] = Field(default_factory=list)


class CashSheetRead(BaseModel):
    """`POST /scans/cash-close` and `GET /scans/cash-close/{id}`."""

    id: UUID
    status: ScanStatus
    model: str
    prompt_version: str
    latency_ms: int | None = None
    error: str | None = None
    #: Sheet-wide, as opposed to the per-day ones inside the draft:
    #: `no_blocks_read`, `already_applied:2026-08-09T20:15:00Z`.
    warnings: list[str] = Field(default_factory=list)
    draft: CashSheetDraft | None = None


# ---------------------------------------------------------------- confirmación


class CashIncomeApply(BaseModel):
    """One collection, as the person confirmed it.

    No serial, no confidence, no read amount: by now the row names a ticket by
    id and an amount in quetzales, and everything that was uncertain about it has
    been settled on screen.
    """

    #: The row it came from, which is what makes the payment's id reproducible.
    index: int
    order_id: UUID
    amount: Decimal = Field(gt=0, le=Decimal("99999999.99"))
    method: PaymentMethod = PaymentMethod.CASH
    #: Whether to hand the clothes back as well. False for a ticket that is being
    #: paid but stays in the shop, which the sheet does record.
    deliver: bool = True


class CashExpenseApply(BaseModel):
    """One expense, as the person confirmed it."""

    index: int
    category_id: UUID
    concept: str = Field(min_length=1, max_length=160)
    amount: Decimal = Field(gt=0, le=Decimal("99999999.99"))
    method: PaymentMethod = PaymentMethod.CASH
    status: ExpenseStatus = ExpenseStatus.PAID
    employee_id: UUID | None = None
    observations: str | None = Field(default=None, max_length=2000)


class CashAttendanceApply(BaseModel):
    """One working day, as the person confirmed it."""

    index: int
    employee_id: UUID
    clock_in: time
    clock_out: time | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CashSheetApply(BaseModel):
    """What to write down from one day of the sheet (§7).

    Only one day per request. A photograph may hold three, and each is its own
    act with its own date lock — importing them in one transaction would make a
    day that is already closed take the two that are not down with it.

    Empty lists are meaningful and allowed: a sheet whose expenses were already
    typed in during the day is imported for its collections alone.
    """

    close_date: date
    incomes: list[CashIncomeApply] = Field(default_factory=list)
    expenses: list[CashExpenseApply] = Field(default_factory=list)
    attendance: list[CashAttendanceApply] = Field(default_factory=list)


class CashAppliedRow(BaseModel):
    """What became of one confirmed row."""

    index: int
    #: `applied`, `skipped` or `failed`. Never a raised exception: one bad row out
    #: of fifteen must not undo the fourteen that were fine, because the person
    #: would have to work out which by hand.
    outcome: str
    #: Coded, for the app to phrase: `already_registered`, `larger_than_balance`,
    #: `order_cancelled`, `day_closed`.
    reason: str | None = None


class CashSheetApplyResult(BaseModel):
    """`POST /scans/cash-close/{id}/apply`."""

    close_date: date
    payments_applied: int = 0
    orders_delivered: int = 0
    expenses_created: int = 0
    attendance_created: int = 0
    collected_total: Decimal = Decimal(0)
    expenses_total: Decimal = Decimal(0)
    incomes: list[CashAppliedRow] = Field(default_factory=list)
    expenses: list[CashAppliedRow] = Field(default_factory=list)
    attendance: list[CashAppliedRow] = Field(default_factory=list)
