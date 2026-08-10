"""Turning a reading of the daily sheet into something a person can confirm.

Pure logic, like `normalization` for the ticket: no session, no HTTP, no
provider. The caller does the queries and passes in the catalog of categories and
the staff; what comes back is the sheet with every row judged.

The judging is the point. On the ticket, what protects the shop from a misread
digit is the pricing engine — the paper's numbers are never charged (Plan 0003
D4). There is no such engine for a day's cash: the sheet *is* the record. So the
guard here is different in kind and it lives in this file:

* an income row is only worth anything once it names a ticket that exists, and
  what it can collect is bounded by the balance that ticket actually owes;
* an expense row is only worth anything once its words point at a category
  somebody administers;
* both come back with a coded `status` that says exactly what was found, so the
  screen can put the doubtful rows in front of the person instead of averaging
  them into a total.

Warnings travel as codes (`income_sum_mismatch:1130:1095`), the way the sync
engine, the daily close and the ticket scan already do it.
"""

from __future__ import annotations

import re
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

from src.modules.expenses.models import ExpenseCategory, ExpenseStatus
from src.modules.intake_scan.cash_schemas import (
    EXPENSE_INCOMPLETE,
    EXPENSE_MATCHED,
    EXPENSE_NO_CATEGORY,
    INCOME_AMOUNT_MISMATCH,
    INCOME_CANCELLED,
    INCOME_MATCHED,
    INCOME_NOT_FOUND,
    INCOME_SETTLED,
    INCOME_SUPPLY,
    INCOME_UNREADABLE,
    CashAttendanceRow,
    CashExpenseRow,
    CashIncomeRow,
    RawCashAttendance,
    RawCashExpense,
    RawCashIncome,
    RawCashSheet,
)
from src.modules.intake_scan.normalization import (
    draft_field,
    normalize_name,
    normalize_serial,
)
from src.modules.orders.models import Order, OrderStatus, PaymentMethod
from src.modules.staff.models import Employee

#: Above this gap between a written sum and the rows above it, the counter is
#: told. Q1 absorbs the rounding of a handwritten column, exactly as the ticket's
#: `TOTAL_TOLERANCE` does.
SUM_TOLERANCE = Decimal("1.00")

#: The highlighter, decoded (Plan 0005 §1).
MARK_TRANSFER = "transfer"
MARK_INVOICE = "invoice"
MARK_SUPPLY = "supply"

#: The sheet's vocabulary for expenses, mapped onto the categories the seeder
#: creates. This is the whole of that translation and it is the first thing to
#: revisit when a category is renamed from the app.
#:
#: Matched on **whole words** after accents and case are stripped, never as
#: substrings: «gas» inside «gastos» is not a purchase of gas, and «moto» inside
#: «motor» is not a courier.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Horas extra": ("extra", "extras", "hora", "horas", "sobretiempo"),
    "Gas": ("gas", "cilindro", "chubb", "zeta"),
    "Transporte (moto)": ("moto", "motorista", "transporte", "pasaje", "combustible"),
    "Compra de insumos": (
        "detergente",
        "detergentes",
        "suavizante",
        "suavitel",
        "cloro",
        "jabon",
        "toallitas",
        "bolsas",
        "insumo",
        "insumos",
        "desinfectante",
    ),
    "Mantenimiento de equipo": (
        "secadora",
        "lavadora",
        "reparacion",
        "repuesto",
        "mantenimiento",
        "tecnico",
        "servicio",
    ),
}

#: Words in the notes column that mean the money has not left the drawer yet.
#: Plan 0005 §5.3: the sheet still counts them for the day.
PENDING_WORDS = ("pendiente", "atrasado", "atrasada", "realizar", "deber", "debe")

#: Words in the notes column that mean it was not cash.
TRANSFER_WORDS = ("transferencia", "transfer", "deposito", "banco", "linea")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _words(text: str | None) -> set[str]:
    """The words of a phrase, stripped of accents, case and punctuation."""
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", normalize_name(text)))


def resolve_sheet_date(
    day: int | None, month: int | None, year: int | None, *, today: date
) -> tuple[date, float, list[str]]:
    """The block's date, or today (Plan 0005 §1).

    The sheet is written `04-08-26`, so a two-digit year is the normal case and
    not an error. It is widened by century rather than guessed at: `26` is 2026
    because this shop's paper does not go back to 1926, and a year the model read
    as three digits is a misreading that falls through to today with a warning.

    Falling back to **today** matters more here than on a ticket. A block whose
    date could not be read still holds a day's collections, and the person is
    standing in front of the sheet: they can see which day it is, and the screen
    puts the date at the top where it is corrected in one tap.
    """
    if day is None or month is None or year is None:
        return today, 0.0, ["date_unreadable"]

    if year < 100:
        year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return today, 0.0, [f"date_unreadable:{year}-{month}-{day}"]

    warnings: list[str] = []
    if parsed != today:
        # Not an error and not rare — the sheet of a Saturday is often typed up on
        # Monday. It is said out loud because of what it does to the money:
        # `OrdersService.add_payment` stamps a collection with *today*, so
        # importing an old sheet books its cash on today's close (Plan 0001 D1).
        warnings.append(f"sheet_not_today:{parsed.isoformat()}")
    return parsed, 1.0, warnings


def decode_mark(raw: str | None) -> tuple[PaymentMethod, bool, bool]:
    """The highlighter, as (method, invoice requested, is a supply sale).

    Anything the prompt's closed list does not contain reads as no mark at all,
    which is the safe default: an unrecognised colour becomes an ordinary cash
    collection that the person sees and can change, never a transfer that
    silently misses the till.
    """
    value = (raw or "").strip().lower()
    return (
        PaymentMethod.TRANSFER if value == MARK_TRANSFER else PaymentMethod.CASH,
        value == MARK_INVOICE,
        value == MARK_SUPPLY,
    )


def parse_clock(raw: str | None) -> time | None:
    """`7:00`, `7.15`, `12.40` — the three ways the sheet writes a time.

    A bare dash in the ALMUERZO box is not a time and comes back `None`; so does
    anything with an hour past 23 or a minute past 59, which is a misread digit
    rather than a moment of the day.
    """
    if not raw:
        return None
    match = re.search(r"(\d{1,2})\s*[:.\-]\s*(\d{2})", raw)
    if match is None:
        # `700` and `7` both happen when the pen skips the separator.
        bare = re.fullmatch(r"\s*(\d{1,2})(\d{2})?\s*", raw)
        if bare is None:
            return None
        hour, minute = int(bare.group(1)), int(bare.group(2) or 0)
    else:
        hour, minute = int(match.group(1)), int(match.group(2))

    if hour > 23 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def resolve_shift(clock_in: time | None, clock_out: time | None) -> tuple[time | None, bool]:
    """Read `6:40` as the evening when it is written after a `7:15` morning.

    The sheet is written in twelve-hour time with no am/pm, and the shop's two
    shifts of five hours (Plan 0005 §1) mean a day that starts at 7:15 does not
    end at 6:40 in the morning. `AttendanceCreate` refuses a day that ends before
    it begins, so without this the row could not be saved at all — and the
    alternative, dropping it, would lose the only record of the hours worked.

    Comes back flagged, never silent: adding twelve hours to somebody's working
    day is a guess, and it is the person who confirms it.
    """
    if clock_in is None or clock_out is None or clock_out > clock_in:
        return clock_out, False
    if clock_out.hour < 12:
        return time(hour=clock_out.hour + 12, minute=clock_out.minute), True
    return clock_out, False


def income_row(index: int, raw: RawCashIncome) -> CashIncomeRow:
    """One income row, decoded but not yet matched against anything."""
    method, invoice, is_supply = decode_mark(raw.mark.value)
    serial = normalize_serial(raw.booklet_serial.value)
    amount = _decimal(raw.amount.value)

    return CashIncomeRow(
        index=index,
        booklet_serial=draft_field(
            serial, raw.booklet_serial.confidence, raw.booklet_serial.raw_text
        ),
        customer_text=draft_field(
            raw.customer_text.value, raw.customer_text.confidence, raw.customer_text.raw_text
        ),
        amount_read=draft_field(amount, raw.amount.confidence, raw.amount.raw_text),
        method=method,
        invoice_requested=invoice,
        status=INCOME_SUPPLY if is_supply else INCOME_UNREADABLE,
    )


def settle_income(row: CashIncomeRow, order: Order | None) -> CashIncomeRow:
    """Pair an income row with the ticket it names, and work out what it can collect.

    The clamp on the last line is the safety rail of the whole import. Whatever
    the paper says, what will be charged is never more than the ticket owes:
    `OrdersService._take_payment` refuses the excess anyway, and change handed
    back at the counter is not income — booking it would leave the drawer short
    with nothing to point at.
    """
    if row.status == INCOME_SUPPLY:
        return row
    if row.booklet_serial.value is None:
        row.status = INCOME_UNREADABLE
        return row
    if order is None:
        row.status = INCOME_NOT_FOUND
        return row

    row.order_id = order.id
    row.order_date = order.order_date
    row.daily_number = order.daily_number
    row.order_status = order.status
    row.order_total = order.total
    row.balance = order.balance

    if order.status is OrderStatus.CANCELLED:
        row.status = INCOME_CANCELLED
        return row

    # A ticket that owes nothing can still need handing back: the money came in
    # as an advance when it was taken, and the sheet is recording the delivery.
    row.can_deliver = order.status is not OrderStatus.DELIVERED

    if order.balance <= 0:
        row.status = INCOME_SETTLED
        row.amount_suggested = None
        return row

    read = row.amount_read.value
    if read is None:
        row.status = INCOME_MATCHED
        row.amount_suggested = order.balance
        return row

    row.amount_suggested = min(read, order.balance)
    row.status = (
        INCOME_AMOUNT_MISMATCH
        if abs(read - order.balance) > SUM_TOLERANCE
        else INCOME_MATCHED
    )
    return row


def match_category(
    text: str, categories: list[ExpenseCategory]
) -> tuple[ExpenseCategory | None, str | None]:
    """Which category those words belong to.

    The category's own name wins over the keyword table, and that ordering is
    what keeps this honest as the business renames things: an administrator who
    creates «Suavizante» gets rows filed under it immediately, without anyone
    editing the table above.
    """
    words = _words(text)
    if not words:
        return None, None

    active = [category for category in categories if category.is_active]
    by_name = {normalize_name(category.name): category for category in active}

    written = normalize_name(text)
    if written in by_name:
        return by_name[written], "exact"
    for name, category in by_name.items():
        if name and words & _words(name):
            return category, "exact"

    for category_name, keywords in CATEGORY_KEYWORDS.items():
        if not words & set(keywords):
            continue
        seeded = by_name.get(normalize_name(category_name))
        if seeded is not None:
            return seeded, "keyword"

    return None, None


def match_employee(text: str | None, employees: list[Employee]) -> Employee | None:
    """The person a first name on the sheet points at.

    Whole-word and unambiguous, or nothing. The sheet writes «maria», never a
    full name, and two Marías on staff means the row is for a human to assign —
    guessing which one would pay the wrong person's overtime.
    """
    words = _words(text)
    if not words:
        return None

    hits = [
        employee
        for employee in employees
        if employee.is_active and words & _words(employee.full_name)
    ]
    return hits[0] if len(hits) == 1 else None


def expense_row(
    index: int,
    raw: RawCashExpense,
    *,
    categories: list[ExpenseCategory],
    employees: list[Employee],
) -> CashExpenseRow:
    """One expense row, with its category and — for overtime — its employee.

    The concept is built from both written columns because neither is enough on
    its own: «gas» does not say how much gas, and «2 sac» does not say of what.
    Together they read the way the person wrote them.
    """
    amount = _decimal(raw.amount.value)
    parts = [
        part.strip()
        for part in (raw.description.value, raw.object_text.value)
        if part and part.strip()
    ]
    concept = " ".join(parts)
    confidence = min(raw.description.confidence, raw.amount.confidence)

    notes_words = _words(raw.observations.value)
    status = (
        ExpenseStatus.PENDING
        if notes_words & set(PENDING_WORDS)
        else ExpenseStatus.PAID
    )
    method = (
        PaymentMethod.TRANSFER
        if notes_words & set(TRANSFER_WORDS)
        else PaymentMethod.CASH
    )

    row = CashExpenseRow(
        index=index,
        concept=draft_field(concept or None, confidence, raw.description.raw_text),
        amount=draft_field(amount, raw.amount.confidence, raw.amount.raw_text),
        observations=draft_field(
            raw.observations.value, raw.observations.confidence, raw.observations.raw_text
        ),
        expense_status=status,
        method=method,
    )

    if amount is None or amount <= 0 or not concept:
        row.status = EXPENSE_INCOMPLETE
        return row

    category, matched_on = match_category(concept, categories)
    if category is None:
        row.status = EXPENSE_NO_CATEGORY
        return row

    row.status = EXPENSE_MATCHED
    row.category_id = category.id
    row.category_name = category.name
    row.matched_on = matched_on

    if normalize_name(category.name) == normalize_name("Horas extra"):
        # «extra — Claudia»: the object column names whoever the overtime paid,
        # and filing it against them is what §6.2 asks for.
        employee = match_employee(raw.object_text.value, employees)
        if employee is not None:
            row.employee_id = employee.id
            row.employee_name = employee.full_name

    return row


def attendance_row(
    index: int, raw: RawCashAttendance, *, employees: list[Employee]
) -> CashAttendanceRow:
    """The hours at the foot of a block, against whoever signed them."""
    clock_in = parse_clock(raw.clock_in.value)
    clock_out, adjusted = resolve_shift(clock_in, parse_clock(raw.clock_out.value))

    row = CashAttendanceRow(
        index=index,
        employee_text=draft_field(
            raw.employee_text.value, raw.employee_text.confidence, raw.employee_text.raw_text
        ),
        clock_in=draft_field(clock_in, raw.clock_in.confidence, raw.clock_in.raw_text),
        clock_out=draft_field(
            clock_out,
            # A time that had to be moved into the afternoon is a guess, and the
            # badge on the field is how the person is told to look at it.
            0.5 if adjusted else raw.clock_out.confidence,
            raw.clock_out.raw_text,
        ),
    )

    employee = match_employee(raw.employee_text.value, employees)
    if employee is not None:
        row.employee_id = employee.id
        row.employee_name = employee.full_name
    return row


def check_sum(read: Decimal | None, rows: Decimal, code: str) -> list[str]:
    """A written column total against what its rows actually add up to.

    Worth showing for the same reason as the ticket's `check_total`: a gap points
    at the row to look at, not at the arithmetic. It is nearly always a line the
    model missed entirely or a digit it read wrong.
    """
    if read is None or read <= 0:
        return []
    if abs(read - rows) > SUM_TOLERANCE:
        return [f"{code}:{read}:{rows}"]
    return []


def sheet_warnings(raw: RawCashSheet) -> list[str]:
    """What is wrong with the photograph as a whole, before any block is read."""
    if not raw.blocks:
        return ["no_blocks_read"]
    return []
