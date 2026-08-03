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
from src.modules.orders.models import (
    Order,
    OrderCharge,
    OrderDiscount,
    OrderGarment,
    OrderPayment,
)


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
    # An order is only ever created whole. There is no generic `update`: each
    # thing that may happen to a ticket is its own operation because each one
    # demands a different permission, and a single `update` would have let
    # anyone who can edit a ticket also deliver it (Plan 0001 §9).
    ("order", "create"): "orders.create",
    ("order", "status"): "orders.update",
    ("order", "deliver"): "orders.deliver",
    ("order", "cancel"): "orders.cancel",
    # Its own entity and not `("order", "payment")`: what the operation creates
    # is a payment, and `entity_id` is the id the device minted for it — which
    # is what makes a retried push idempotent.
    ("order_payment", "create"): "orders.collect_payment",
}
