"""Idempotency and conflict detection — the two promises of Plan 0004 §7.1."""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError
from src.modules.customers.models import Customer
from src.modules.customers.service import CustomersService
from src.modules.orders.models import Order, OrderPayment, OrderStatus, PaymentMethod
from src.modules.orders.schemas import OrderCancel, OrderDeliver, OrderPaymentPush
from src.modules.sync.models import OperationStatus, SyncDevice, SyncOperation
from src.modules.sync.registry import FEED_ENTITIES_BY_NAME, OPERATION_PERMISSIONS
from src.modules.sync.schemas import SyncOperationIn
from src.modules.sync.service import SyncService


def operation(status: OperationStatus, **kwargs: object) -> SyncOperation:
    return SyncOperation(
        op_id=kwargs.get("op_id", uuid4()),
        device_id=uuid4(),
        user_id=uuid4(),
        entity="customer",
        op_type="create",
        entity_id=kwargs.get("entity_id", uuid4()),
        payload={},
        status=status,
        result=kwargs.get("result"),
        reason=kwargs.get("reason"),
    )


class TestReplay:
    def test_an_applied_operation_replays_as_already_applied(self) -> None:
        """A retry after a mid-sync crash must not create a second customer."""
        recorded = operation(
            OperationStatus.APPLIED, result={"id": str(uuid4()), "version": 1}
        )

        result = SyncService._replay(recorded)

        assert result.status is OperationStatus.ALREADY_APPLIED
        assert result.server_version == 1
        assert result.entity_id == recorded.entity_id

    def test_a_rejection_replays_as_a_rejection(self) -> None:
        """Retrying a rejected operation must not quietly turn into a success."""
        recorded = operation(OperationStatus.REJECTED, reason="boleta ya registrada")

        result = SyncService._replay(recorded)

        assert result.status is OperationStatus.REJECTED
        assert result.reason == "boleta ya registrada"

    def test_a_conflict_replays_as_a_conflict(self) -> None:
        recorded = operation(OperationStatus.CONFLICT, reason="version mismatch")

        assert SyncService._replay(recorded).status is OperationStatus.CONFLICT

    def test_warnings_survive_the_replay(self) -> None:
        duplicate_id = uuid4()
        recorded = operation(
            OperationStatus.APPLIED,
            result={"version": 1, "warnings": [f"possible_duplicate_of:{duplicate_id}"]},
        )

        result = SyncService._replay(recorded)

        assert result.warnings == [f"possible_duplicate_of:{duplicate_id}"]


class TestOptimisticConcurrency:
    def test_matching_version_is_accepted(self) -> None:
        customer = Customer(full_name="Marta González")
        customer.version = 3

        CustomersService.ensure_version(customer, 3)

    def test_stale_version_is_a_conflict(self) -> None:
        """Someone else edited the row while this device was offline (D6)."""
        customer = Customer(full_name="Marta González")
        customer.version = 5

        with pytest.raises(ConflictError) as error:
            CustomersService.ensure_version(customer, 3)

        assert "5" in str(error.value)

    def test_no_base_version_skips_the_check(self) -> None:
        """A plain admin edit over REST is not doing optimistic concurrency."""
        customer = Customer(full_name="Marta González")
        customer.version = 5

        CustomersService.ensure_version(customer, None)


class TestRegistry:
    def test_the_catalog_is_readable_but_not_writable_from_a_device(self) -> None:
        """Plan 0004 §7.3: catalog flows server to app only."""
        writable = {entity for entity, _ in OPERATION_PERMISSIONS}

        assert "customer" in writable
        for catalog_entity in ("service_type", "service_option", "service_price", "garment_type"):
            assert catalog_entity in FEED_ENTITIES_BY_NAME
            assert catalog_entity not in writable

    def test_archiving_a_customer_demands_its_own_permission(self) -> None:
        """Plan 0006 §13: a collaborator may edit a customer but not retire one.

        Were archiving pushed as an update carrying `is_active: false`, it would
        pass the `customers.update` check every collaborator holds.
        """
        assert OPERATION_PERMISSIONS[("customer", "archive")] == "customers.archive"
        assert OPERATION_PERMISSIONS[("customer", "update")] == "customers.update"

    def test_every_writable_entity_is_also_in_the_feed(self) -> None:
        """An operation whose result cannot be serialized would 500 on success."""
        for entity, _ in OPERATION_PERMISSIONS:
            assert entity in FEED_ENTITIES_BY_NAME, entity

    def test_an_order_has_no_generic_update(self) -> None:
        """Plan 0001 §9: delivering, voiding and editing are three permissions.

        A single `("order", "update")` would have let anyone who may edit a ticket
        also hand it over.
        """
        order_ops = {op_type for entity, op_type in OPERATION_PERMISSIONS if entity == "order"}

        assert order_ops == {"create", "status", "deliver", "cancel"}
        assert OPERATION_PERMISSIONS[("order", "deliver")] == "orders.deliver"
        assert OPERATION_PERMISSIONS[("order", "cancel")] == "orders.cancel"
        assert OPERATION_PERMISSIONS[("order_payment", "create")] == "orders.collect_payment"

    def test_a_ticket_travels_as_five_rows(self) -> None:
        """Each table has its own `sync_seq`, so a payment reaches the devices
        without re-sending the order it belongs to."""
        for entity in ("order", "order_garment", "order_charge", "order_discount", "order_payment"):
            assert entity in FEED_ENTITIES_BY_NAME, entity
            assert "order_id" in FEED_ENTITIES_BY_NAME[entity].fields or entity == "order"

    def test_an_order_travels_with_the_money_and_the_life_cycle(self) -> None:
        order = Order(
            order_date=date(2026, 7, 20),
            daily_number=4,
            customer_id=uuid4(),
            total_pieces=6,
            status=OrderStatus.RECEIVED,
            subtotal=Decimal("116.25"),
            discount_total=Decimal("7.50"),
            total=Decimal("108.75"),
            received_by_id=uuid4(),
        )
        order.id = uuid4()
        order.version = 1

        data = FEED_ENTITIES_BY_NAME["order"].serialize(order)

        # Money as strings, for the same reason as everywhere else.
        assert data["total"] == "108.75"
        assert data["status"] == "received"
        assert data["order_date"] == "2026-07-20"
        # The device needs these to draw a delivered ticket without asking again.
        for field in ("delivered_at", "cancelled_at", "cancel_reason"):
            assert field in data

    def test_serialization_renders_money_and_ids_as_strings(self) -> None:
        """JSON floats would round quetzales somewhere between here and the device."""
        customer = Customer(full_name="Marta González", phone="4815-2964")
        customer.id = uuid4()
        customer.version = 1

        data = FEED_ENTITIES_BY_NAME["customer"].serialize(customer)

        assert data["id"] == str(customer.id)
        assert data["full_name"] == "Marta González"
        assert data["phone"] == "4815-2964"

    def test_the_feed_never_exposes_columns_that_were_not_declared(self) -> None:
        customer = Customer(full_name="Marta González")
        customer.id = uuid4()
        customer.version = 1
        customer.deleted_at = datetime.now(UTC)

        data = FEED_ENTITIES_BY_NAME["customer"].serialize(customer)

        # `deleted` travels as its own field of the change envelope, not as a column.
        assert "deleted_at" not in data
        assert "sync_seq" not in data


class FakeOrdersService:
    """Records what the applicator asked for, and hands back a plausible ticket."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.order = Order(
            order_date=date(2026, 7, 20),
            daily_number=4,
            customer_id=uuid4(),
            total_pieces=6,
            status=OrderStatus.RECEIVED,
            subtotal=Decimal("108.75"),
            discount_total=Decimal("0.00"),
            total=Decimal("108.75"),
            received_by_id=uuid4(),
        )
        self.order.id = uuid4()
        self.order.version = 1

    async def create(self, data: object, *, actor: object) -> tuple[Order, list[str]]:
        del actor
        self.calls.append(("create", data))
        return self.order, ["hand_wash_out_of_range"]

    async def change_status(self, order_id: UUID, status: OrderStatus, *, actor: object) -> Order:
        del actor
        self.calls.append(("status", (order_id, status)))
        return self.order

    async def deliver(self, order_id: UUID, data: object, *, actor: object) -> Order:
        del actor
        self.calls.append(("deliver", (order_id, data)))
        return self.order

    async def cancel(self, order_id: UUID, data: object, *, actor: object) -> Order:
        del actor
        self.calls.append(("cancel", (order_id, data)))
        return self.order

    async def add_payment(self, order_id: UUID, data: OrderPaymentPush, *, actor: object) -> Order:
        del actor
        self.calls.append(("payment", (order_id, data)))
        payment = OrderPayment(
            amount=data.amount,
            method=data.method,
            is_advance=data.is_advance,
            received_by_id=uuid4(),
        )
        assert data.id is not None  # the applicator always supplies `entity_id`
        payment.id = data.id
        payment.version = 1
        self.order.payments.append(payment)
        return self.order


class TestOrderApplicator:
    """Plan 0004 D2: an operation is a command replayed through the same service.

    What is asserted here is only the routing — that each op reaches the right
    method with the right arguments. The rules themselves belong to (and are
    tested with) the orders service.
    """

    @staticmethod
    def _service() -> tuple[SyncService, FakeOrdersService]:
        orders = FakeOrdersService()
        return SyncService(None, None, orders, None), orders  # type: ignore[arg-type]

    @staticmethod
    def _incoming(
        entity: str, op_type: str, payload: dict[str, Any], entity_id: UUID
    ) -> SyncOperationIn:
        return SyncOperationIn(
            op_id=uuid4(),
            seq=1,
            entity=entity,
            op_type=op_type,
            entity_id=entity_id,
            payload=payload,
        )

    async def test_a_captured_ticket_keeps_the_id_the_device_minted(self) -> None:
        """A ticket printed offline has to name the same order the server stores."""
        service, orders = self._service()
        offline_id = uuid4()
        operation = self._incoming(
            "order",
            "create",
            {
                "customer_id": str(uuid4()),
                "charges": [{"service_code": "wash_tub", "option_code": "G"}],
            },
            offline_id,
        )

        version, data, warnings = await service._apply_order(None, operation)  # type: ignore[arg-type]

        assert orders.calls[0][0] == "create"
        assert orders.calls[0][1].id == offline_id
        assert version == 1
        assert data["total"] == "108.75"
        # The engine's warnings reach the device instead of dying at the endpoint.
        assert warnings == ["hand_wash_out_of_range"]

    async def test_a_status_change_carries_the_target(self) -> None:
        service, orders = self._service()
        order_id = uuid4()

        await service._apply_order(
            None,  # type: ignore[arg-type]
            self._incoming("order", "status", {"status": "in_progress"}, order_id),
        )

        assert orders.calls[0] == ("status", (order_id, OrderStatus.IN_PROGRESS))

    async def test_a_delivery_carries_the_garment_counts(self) -> None:
        service, orders = self._service()
        order_id = uuid4()
        garment_id = uuid4()

        await service._apply_order(
            None,  # type: ignore[arg-type]
            self._incoming(
                "order",
                "deliver",
                {"garments": [{"garment_type_id": str(garment_id), "quantity_delivered": 3}]},
                order_id,
            ),
        )

        _, (target, delivery) = orders.calls[0]
        assert target == order_id
        assert isinstance(delivery, OrderDeliver)
        assert delivery.garments[0].quantity_delivered == 3

    async def test_a_cancellation_carries_the_reason(self) -> None:
        service, orders = self._service()

        await service._apply_order(
            None,  # type: ignore[arg-type]
            self._incoming("order", "cancel", {"reason": "Cliente se arrepintió"}, uuid4()),
        )

        _, (_, cancellation) = orders.calls[0]
        assert isinstance(cancellation, OrderCancel)
        assert cancellation.reason == "Cliente se arrepintió"

    async def test_a_payment_answers_with_the_payment_it_created(self) -> None:
        """`entity_id` is the payment's, so the device can settle its own row."""
        service, orders = self._service()
        order_id = uuid4()
        payment_id = uuid4()

        version, data, _ = await service._apply_order(
            None,  # type: ignore[arg-type]
            self._incoming(
                "order_payment",
                "create",
                {"order_id": str(order_id), "amount": "40.00", "method": "transfer"},
                payment_id,
            ),
        )

        _, (target, push) = orders.calls[0]
        assert target == order_id
        assert push.id == payment_id
        assert push.method is PaymentMethod.TRANSFER
        assert version == 1
        assert data["id"] == str(payment_id)
        assert data["amount"] == "40.00"


class TestClockSkew:
    """A diagnostic must never be able to take down the operation it observes."""

    @staticmethod
    def _incoming(client_ts: datetime | None) -> SyncOperationIn:
        return SyncOperationIn(
            op_id=uuid4(),
            seq=1,
            entity="customer",
            op_type="create",
            entity_id=uuid4(),
            payload={},
            client_ts=client_ts,
        )

    def test_a_timestamp_without_an_offset_does_not_break_the_push(self) -> None:
        """A naive `client_ts` used to raise TypeError and 500 the whole batch."""
        device = SyncDevice(id=uuid4(), user_id=uuid4(), name="Tablet mostrador")

        SyncService._warn_on_clock_skew(device, self._incoming(datetime.now()))

    def test_an_off_clock_is_reported_and_still_applies(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = SyncDevice(id=uuid4(), user_id=uuid4(), name="Tablet mostrador")
        way_off = datetime.now(UTC) - timedelta(hours=9)

        with caplog.at_level(logging.WARNING):
            SyncService._warn_on_clock_skew(device, self._incoming(way_off))

        assert "sync.clock_skew" in caplog.text

    def test_no_timestamp_is_not_a_problem(self) -> None:
        device = SyncDevice(id=uuid4(), user_id=uuid4(), name="Tablet mostrador")

        SyncService._warn_on_clock_skew(device, self._incoming(None))
