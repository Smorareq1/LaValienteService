"""What travels down the change feed, and what may travel up.

Two registries live here:

* :data:`FEED_ENTITIES` — the tables a device mirrors locally. Adding a module to
  the pull is one entry, not a new endpoint.
* :data:`OPERATION_PERMISSIONS` — the permission each writable operation demands.
  RBAC is evaluated when the operation is *applied*, not when it was captured
  (Plan 0004 §10), so a permission withdrawn while a device was offline still
  takes effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from src.modules.catalog.models import GarmentType, ServiceOption, ServicePrice, ServiceType
from src.modules.customers.models import Customer
from src.modules.daily_close.models import DailyClosure
from src.modules.expenses.models import Expense, ExpenseCategory
from src.modules.identity.permissions import ensure_catalogued
from src.modules.inventory.models import (
    InventoryMovement,
    Product,
    ProductLot,
    SupplySale,
    SupplySaleItem,
)
from src.modules.orders.models import (
    Order,
    OrderCharge,
    OrderDiscount,
    OrderGarment,
    OrderPayment,
)
from src.modules.promotions.models import Promotion
from src.modules.staff.models import AttendanceRecord, Employee, PayrollRate, WorkShift


def _jsonable(value: Any) -> Any:
    """Render a column value in a form the JSON response can carry.

    Money stays a string: a float would quietly round Q2.50 somewhere between
    here and the device.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class FeedEntity:
    """One table in the change feed."""

    name: str
    model: type[Any]
    #: Columns sent to devices. Listed explicitly so adding a server-side column
    #: never leaks into the wire format by accident.
    fields: tuple[str, ...]

    def serialize(self, row: Any) -> dict[str, Any]:
        return {field: _jsonable(getattr(row, field)) for field in self.fields}


_COMMON = ("id", "version")

FEED_ENTITIES: tuple[FeedEntity, ...] = (
    FeedEntity(
        name="service_type",
        model=ServiceType,
        fields=(*_COMMON, "code", "name", "pricing_mode", "unit_label", "is_active", "sort_order"),
    ),
    FeedEntity(
        name="service_option",
        model=ServiceOption,
        fields=(
            *_COMMON,
            "service_type_id",
            "code",
            "name",
            "min_quantity",
            "max_quantity",
            "is_active",
            "sort_order",
        ),
    ),
    FeedEntity(
        name="service_price",
        model=ServicePrice,
        fields=(
            *_COMMON,
            "service_type_id",
            "service_option_id",
            "price",
            "valid_from",
            "valid_to",
        ),
    ),
    FeedEntity(
        name="garment_type",
        model=GarmentType,
        fields=(*_COMMON, "name", "notes", "is_active", "sort_order"),
    ),
    # Server to app only, like the catalog (Plan 0004 §7.3): the app has to know
    # which promotions are live to offer them while offline, and never writes one.
    FeedEntity(
        name="promotion",
        model=Promotion,
        fields=(
            *_COMMON,
            "code",
            "name",
            "description",
            "discount_type",
            "value",
            "applies_to_service_codes",
            "valid_from",
            "valid_to",
            "is_active",
        ),
    ),
    FeedEntity(
        name="customer",
        model=Customer,
        fields=(
            *_COMMON,
            "full_name",
            "phone",
            "nit",
            "email",
            "address",
            "notes",
            "is_active",
        ),
    ),
    # A ticket travels as five rows, not as one nested document: each table
    # carries its own `sync_seq`, so a payment taken today reaches the devices
    # without re-sending the order it belongs to. The device stitches them back
    # together by `order_id`.
    FeedEntity(
        name="order",
        model=Order,
        fields=(
            *_COMMON,
            "order_date",
            "daily_number",
            "booklet_serial",
            "customer_id",
            "nit",
            "weight_lbs",
            "total_pieces",
            "observations",
            "status",
            "subtotal",
            "discount_total",
            "total",
            "received_by_id",
            "delivered_at",
            "delivered_by_id",
            "cancelled_at",
            "cancelled_by_id",
            "cancel_reason",
            # La hora a la que se recibió. Es el único campo de auditoría que
            # viaja, y viaja porque la lista del día lo muestra en cada tarjeta
            # (Plan 0006 §5.1): en el mostrador los pedidos se distinguen por la
            # hora tanto como por el número.
            "created_at",
        ),
    ),
    FeedEntity(
        name="order_garment",
        model=OrderGarment,
        fields=(
            *_COMMON,
            "order_id",
            "garment_type_id",
            "quantity",
            "quantity_delivered",
            "notes",
        ),
    ),
    FeedEntity(
        name="order_charge",
        model=OrderCharge,
        fields=(
            *_COMMON,
            "order_id",
            "service_type_id",
            "service_option_id",
            "description",
            "quantity",
            "unit_price",
            "amount",
        ),
    ),
    FeedEntity(
        name="order_discount",
        model=OrderDiscount,
        fields=(*_COMMON, "order_id", "promotion_id", "description", "amount"),
    ),
    FeedEntity(
        name="order_payment",
        model=OrderPayment,
        fields=(
            *_COMMON,
            "order_id",
            "amount",
            "method",
            "is_advance",
            "reference",
            "received_by_id",
            "paid_at",
        ),
    ),
    # -- the daily register (Plan 0005 §6.4) -------------------------------
    #
    # Everything below travels down. Only three of them travel back up, and
    # those are in `OPERATION_PERMISSIONS`: the rest is administered online
    # (D11), which is why a device mirrors them and never writes one.
    FeedEntity(
        name="employee",
        model=Employee,
        fields=(*_COMMON, "full_name", "phone", "user_id", "notes", "is_active"),
    ),
    FeedEntity(
        name="work_shift",
        model=WorkShift,
        fields=(*_COMMON, "code", "name", "starts_at", "ends_at", "is_active", "sort_order"),
    ),
    # The app prices the overtime it suggests, so it needs the windows and not
    # just today's amount — the same reason `service_price` travels whole.
    FeedEntity(
        name="payroll_rate",
        model=PayrollRate,
        fields=(*_COMMON, "code", "amount", "valid_from", "valid_to"),
    ),
    FeedEntity(
        name="attendance_record",
        model=AttendanceRecord,
        fields=(
            *_COMMON,
            "employee_id",
            "work_date",
            "shift_id",
            "clock_in",
            "clock_out",
            # The minutes someone confirmed. The suggestion is *not* here and
            # must not be: it is computed on every read precisely so that it
            # cannot be mistaken for a decision (D8).
            "overtime_minutes",
            "notes",
        ),
    ),
    FeedEntity(
        name="expense_category",
        model=ExpenseCategory,
        fields=(*_COMMON, "name", "is_active", "sort_order"),
    ),
    FeedEntity(
        name="expense",
        model=Expense,
        fields=(
            *_COMMON,
            "expense_date",
            "category_id",
            "concept",
            "amount",
            "method",
            "status",
            "employee_id",
            "attendance_record_id",
            "product_lot_id",
            "observations",
            "created_by_id",
            # A voided expense arrives as a tombstone; the reason travels with it
            # so the day's total stays explainable on the device too.
            "void_reason",
        ),
    ),
    FeedEntity(
        name="product",
        model=Product,
        fields=(*_COMMON, "name", "unit", "description", "image_path", "is_active", "sort_order"),
    ),
    FeedEntity(
        name="product_lot",
        model=ProductLot,
        fields=(
            *_COMMON,
            "product_id",
            "lot_number",
            "quantity_received",
            "quantity_available",
            # `unit_cost` is deliberately absent. Nothing the app does offline
            # needs what a bottle cost, and the counter screen is the last place
            # the purchase margin should be readable.
            "sale_price",
            "received_at",
        ),
    ),
    FeedEntity(
        name="supply_sale",
        model=SupplySale,
        fields=(
            *_COMMON,
            "sale_date",
            "customer_id",
            "nit",
            "method",
            "reference",
            "total",
            "sold_by_id",
            "cancelled_at",
            "cancelled_by_id",
            "cancel_reason",
            "created_at",
        ),
    ),
    FeedEntity(
        name="supply_sale_item",
        model=SupplySaleItem,
        fields=(*_COMMON, "sale_id", "lot_id", "description", "quantity", "unit_price", "amount"),
    ),
    # The kardex travels so a device can explain the stock it shows: without the
    # movements, a lot whose quantity dropped overnight is an unexplained number.
    FeedEntity(
        name="inventory_movement",
        model=InventoryMovement,
        fields=(
            *_COMMON,
            "lot_id",
            "movement_type",
            "quantity",
            "unit_price",
            "supply_sale_item_id",
            "notes",
            "created_by_id",
            "created_at",
        ),
    ),
    # Closing is online-only (D11), but the acta has to reach every device: it is
    # what tells them the day is locked before they try to write into it.
    FeedEntity(
        name="daily_closure",
        model=DailyClosure,
        fields=(
            *_COMMON,
            "close_date",
            "orders_income",
            "supplies_income",
            "expenses_total",
            "net_total",
            "cash_income",
            "transfer_income",
            "cash_expenses",
            "transfer_expenses",
            "orders_delivered",
            "notes",
            "closed_by_id",
            "closed_at",
            "reopened_by_id",
            "reopen_reason",
        ),
    ),
)

FEED_ENTITIES_BY_NAME: dict[str, FeedEntity] = {entity.name: entity for entity in FEED_ENTITIES}

#: Operations a device may push, and the permission each one requires.
#: The catalog is server-to-app only (Plan 0004 §7.3), so it appears in the feed
#: but never here.
OPERATION_PERMISSIONS: dict[tuple[str, str], str] = {
    ("customer", "create"): "customers.create",
    ("customer", "update"): "customers.update",
    # Archiving is its own operation rather than an update carrying
    # `is_active: false`, because it demands a different permission (Plan 0006
    # §13: collaborators may edit a customer but not retire one). As an update
    # it would have passed the `customers.update` check that every collaborator
    # holds.
    ("customer", "archive"): "customers.archive",
    # Each thing that may happen to a ticket is its own operation, because each
    # one demands a different permission: a single catch-all `update` would have
    # let anyone who can correct a ticket also deliver it (Plan 0001 §9).
    # `update` is the correction itself — replacing what the boleta says (§7.3),
    # never its status, its delivery or its payments.
    ("order", "create"): "orders.create",
    ("order", "update"): "orders.update",
    ("order", "status"): "orders.update",
    ("order", "deliver"): "orders.deliver",
    ("order", "cancel"): "orders.cancel",
    # Its own entity and not `("order", "payment")`: what the operation creates
    # is a payment, and `entity_id` is the id the device minted for it — which
    # is what makes a retried push idempotent.
    ("order_payment", "create"): "orders.collect_payment",
    # Plan 0005 §6.4. Three of the daily register's tables come back up, and no
    # more: the catalogs behind them are administered online (D11), and closing
    # a day is a decision nobody takes without seeing the whole day first.
    #
    # Clocking in and clocking out are one permission and two operations of the
    # same entity, because the second is an edit of the row the first created —
    # `update` also carries the confirmed overtime minutes (D8).
    ("attendance_record", "create"): "attendance.record",
    ("attendance_record", "update"): "attendance.record",
    ("expense", "create"): "expenses.create",
    ("expense", "update"): "expenses.update",
    # Voiding an expense is absent on purpose: §6.4 allows a device `create` and
    # `update`, and §7 gives `expenses.void` to the administrator alone. Someone
    # who may not void one online may not void one from a queue either.
    ("supply_sale", "create"): "supply_sales.create",
    ("supply_sale", "cancel"): "supply_sales.cancel",
}

# Same guard the route dependencies get: a code nobody seeded would let every
# queued operation of that kind pile up as `rejected` on devices that were
# offline when it was captured, and the administrator testing it would see
# nothing wrong.
ensure_catalogued(*OPERATION_PERMISSIONS.values())
