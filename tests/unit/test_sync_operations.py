"""Idempotency and conflict detection — the two promises of Plan 0004 §7.1."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError, StaleVersionError
from src.modules.customers.models import Customer
from src.modules.customers.service import CustomersService
from src.modules.expenses.models import Expense, ExpenseStatus
from src.modules.inventory.allocation import InsufficientStock
from src.modules.inventory.models import SupplySale
from src.modules.orders.models import Order, OrderPayment, OrderStatus, PaymentMethod
from src.modules.orders.schemas import OrderCancel, OrderDeliver, OrderPaymentPush
from src.modules.staff.models import AttendanceRecord
from src.modules.sync.models import OperationStatus, SyncDevice, SyncOperation
from src.modules.sync.registry import FEED_ENTITIES_BY_NAME, OPERATION_PERMISSIONS
from src.modules.sync.schemas import SyncOperationIn
from src.modules.sync.service import SyncService


def build_service(**services: Any) -> SyncService:
    """A `SyncService` holding only the collaborators a test actually exercises.

    Named rather than positional: every applicator test broke the day sync
    learned about three more modules, and none of them cared.
    """
    slots: dict[str, Any] = dict.fromkeys(
        ("repository", "customers", "orders", "identity", "staff", "expenses", "inventory")
    )
    return SyncService(**{**slots, **services})


def incoming(
    entity: str,
    op_type: str,
    payload: dict[str, Any],
    entity_id: UUID,
    *,
    base_version: int | None = None,
) -> SyncOperationIn:
    return SyncOperationIn(
        op_id=uuid4(),
        seq=1,
        entity=entity,
        op_type=op_type,
        entity_id=entity_id,
        payload=payload,
        base_version=base_version,
    )


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

    def test_each_thing_that_happens_to_a_ticket_is_its_own_operation(self) -> None:
        """Plan 0001 §9: correcting, delivering and voiding are three permissions.

        `update` is the correction of §7.3 and nothing else. Folding delivery or
        cancellation into it would have let anyone who may fix a typo on a ticket
        also hand the clothes over.
        """
        order_ops = {op_type for entity, op_type in OPERATION_PERMISSIONS if entity == "order"}

        assert order_ops == {"create", "update", "status", "deliver", "cancel"}
        assert OPERATION_PERMISSIONS[("order", "update")] == "orders.update"
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
        return build_service(orders=orders), orders

    _incoming = staticmethod(incoming)

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


class TestTheDailyRegisterInTheFeed:
    """Plan 0005 §6.4: which of the new tables travel, and in which direction."""

    MIRRORED = (
        "employee",
        "work_shift",
        "payroll_rate",
        "attendance_record",
        "expense_category",
        "expense",
        "product",
        "product_lot",
        "supply_sale",
        "supply_sale_item",
        "inventory_movement",
        "daily_closure",
    )

    def test_every_table_of_the_daily_register_reaches_the_devices(self) -> None:
        for entity in self.MIRRORED:
            assert entity in FEED_ENTITIES_BY_NAME, entity

    def test_only_three_of_them_come_back_up(self) -> None:
        """The rest is administered online (D11), so a device mirrors and never writes."""
        writable = {entity for entity, _ in OPERATION_PERMISSIONS}

        assert writable & set(self.MIRRORED) == {"attendance_record", "expense", "supply_sale"}

    def test_closing_a_day_is_never_pushed(self) -> None:
        """D11: the acta travels down so devices learn the day is locked, and up
        never — nobody closes a day without seeing the whole day first."""
        assert "daily_closure" in FEED_ENTITIES_BY_NAME
        assert not any(entity == "daily_closure" for entity, _ in OPERATION_PERMISSIONS)

    def test_voiding_an_expense_is_not_a_device_operation(self) -> None:
        """§6.4 gives a device `create` and `update`; §7 keeps `void` for the admin."""
        expense_ops = {op_type for entity, op_type in OPERATION_PERMISSIONS if entity == "expense"}

        assert expense_ops == {"create", "update"}

    def test_what_a_bottle_cost_never_leaves_the_server(self) -> None:
        """Nothing the app does offline needs the purchase price, and the counter
        screen is the last place the margin should be readable."""
        assert "sale_price" in FEED_ENTITIES_BY_NAME["product_lot"].fields
        assert "unit_cost" not in FEED_ENTITIES_BY_NAME["product_lot"].fields

    def test_the_suggested_overtime_is_not_in_the_feed(self) -> None:
        """D8: only confirmed minutes are a decision, and only decisions travel."""
        fields = FEED_ENTITIES_BY_NAME["attendance_record"].fields

        assert "overtime_minutes" in fields
        assert "suggested_overtime_minutes" not in fields

    def test_an_expense_travels_with_its_money_and_its_links(self) -> None:
        expense = Expense(
            expense_date=date(2026, 7, 18),
            category_id=uuid4(),
            concept="Gas — 2 sacos",
            amount=Decimal("161.00"),
            method=PaymentMethod.CASH,
            status=ExpenseStatus.PAID,
            created_by_id=uuid4(),
        )
        expense.id = uuid4()
        expense.version = 1

        data = FEED_ENTITIES_BY_NAME["expense"].serialize(expense)

        assert data["amount"] == "161.00"
        assert data["method"] == "cash"
        assert data["status"] == "paid"
        assert data["expense_date"] == "2026-07-18"
        # Absent links come across as null and not as missing keys: the device
        # mirrors a fixed set of columns.
        assert data["attendance_record_id"] is None

    def test_a_working_day_travels_as_wall_clock_times(self) -> None:
        record = AttendanceRecord(
            employee_id=uuid4(),
            work_date=date(2026, 7, 18),
            clock_in=time(6, 50),
            clock_out=time(13, 0),
            overtime_minutes=60,
        )
        record.id = uuid4()
        record.version = 2

        data = FEED_ENTITIES_BY_NAME["attendance_record"].serialize(record)

        assert data["clock_in"] == "06:50:00"
        assert data["clock_out"] == "13:00:00"
        assert data["overtime_minutes"] == 60


class FakeStaffService:
    """Records the calls and hands back a working day the feed can serialize."""

    def __init__(self, *, fails_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fails_with = fails_with
        self.record = AttendanceRecord(
            employee_id=uuid4(),
            work_date=date(2026, 7, 18),
            clock_in=time(6, 50),
            overtime_minutes=0,
        )
        self.record.id = uuid4()
        self.record.version = 1

    async def clock_in(self, data: Any) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("clock_in", data))
        self.record.id = data.id

    async def update_attendance(
        self, record_id: UUID, data: Any, *, base_version: int | None = None
    ) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("update", (record_id, data, base_version)))
        self.record.version = 2

    async def get_attendance(self, record_id: UUID) -> AttendanceRecord:
        self.record.id = record_id
        return self.record


class FakeExpensesService:
    def __init__(self, *, fails_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fails_with = fails_with
        self.expense = Expense(
            expense_date=date(2026, 7, 18),
            category_id=uuid4(),
            concept="Gas — 2 sacos",
            amount=Decimal("161.00"),
            method=PaymentMethod.CASH,
            status=ExpenseStatus.PAID,
            created_by_id=uuid4(),
        )
        self.expense.id = uuid4()
        self.expense.version = 1

    async def create_expense(self, data: Any, *, actor: Any) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("create", (data, actor)))

    async def update_expense(
        self, expense_id: UUID, data: Any, *, base_version: int | None = None
    ) -> None:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("update", (expense_id, data, base_version)))

    async def get_expense(self, expense_id: UUID) -> Expense:
        self.expense.id = expense_id
        return self.expense


class FakeInventoryService:
    def __init__(self, *, fails_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fails_with = fails_with
        self.sale = SupplySale(
            sale_date=date(2026, 7, 18),
            method=PaymentMethod.CASH,
            # What the shelf actually charges, which is not what the device sent.
            total=Decimal("140.00"),
            sold_by_id=uuid4(),
        )
        self.sale.id = uuid4()
        self.sale.version = 1

    async def create_sale(self, data: Any, *, actor: Any) -> SupplySale:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("create", (data, actor)))
        self.sale.id = data.id
        return self.sale

    async def cancel_sale(self, sale_id: UUID, data: Any, *, actor: Any) -> SupplySale:
        if self.fails_with is not None:
            raise self.fails_with
        self.calls.append(("cancel", (sale_id, data, actor)))
        self.sale.id = sale_id
        return self.sale


class TestDailyRegisterApplicators:
    """Plan 0004 D2 again: each op reaches the service the REST API would use.

    The rules live with those services and are tested there; what is checked here
    is the routing, the device-minted id and the version travelling through.
    """

    async def test_a_working_day_clocked_in_offline_keeps_its_id(self) -> None:
        staff = FakeStaffService()
        service = build_service(staff=staff)
        offline_id = uuid4()

        version, data, _ = await service._apply_attendance(
            incoming(
                "attendance_record",
                "create",
                {
                    "employee_id": str(uuid4()),
                    "work_date": "2026-07-18",
                    "clock_in": "06:50:00",
                },
                offline_id,
            )
        )

        assert staff.calls[0][0] == "clock_in"
        assert staff.calls[0][1].id == offline_id
        assert version == 1
        assert data["id"] == str(offline_id)

    async def test_clocking_out_declares_the_version_it_was_built_on(self) -> None:
        """Two devices clocking the same person out is what `base_version` is for."""
        staff = FakeStaffService()
        service = build_service(staff=staff)
        record_id = uuid4()

        await service._apply_attendance(
            incoming(
                "attendance_record",
                "update",
                {"clock_out": "13:00:00", "overtime_minutes": 60},
                record_id,
                base_version=1,
            )
        )

        _, (target, changes, base_version) = staff.calls[0]
        assert target == record_id
        assert changes.overtime_minutes == 60
        assert base_version == 1

    async def test_an_expense_captured_offline_keeps_its_id_and_its_author(self) -> None:
        expenses = FakeExpensesService()
        service = build_service(expenses=expenses)
        actor = object()
        offline_id = uuid4()

        _, data, _ = await service._apply_expense(
            actor,  # type: ignore[arg-type]
            incoming(
                "expense",
                "create",
                {
                    "category_id": str(uuid4()),
                    "concept": "Gas — 2 sacos",
                    "amount": "161.00",
                    "expense_date": "2026-07-18",
                },
                offline_id,
            ),
        )

        _, (creation, captured_by) = expenses.calls[0]
        assert creation.id == offline_id
        # The operation runs as whoever captured it, not whoever is holding the
        # device now (Plan 0004 §10).
        assert captured_by is actor
        assert data["amount"] == "161.00"

    async def test_correcting_an_expense_declares_its_version(self) -> None:
        expenses = FakeExpensesService()
        service = build_service(expenses=expenses)
        expense_id = uuid4()

        await service._apply_expense(
            None,  # type: ignore[arg-type]
            incoming("expense", "update", {"amount": "170.00"}, expense_id, base_version=3),
        )

        _, (target, changes, base_version) = expenses.calls[0]
        assert target == expense_id
        assert changes.amount == Decimal("170.00")
        assert base_version == 3

    async def test_a_counter_sale_comes_back_with_the_servers_figures(self) -> None:
        """D10 of Plan 0004: the price list may have moved while the device was away.

        The device sends products and quantities and never a total, so what comes
        back is what the shelf actually charged.
        """
        inventory = FakeInventoryService()
        service = build_service(inventory=inventory)
        offline_id = uuid4()

        version, data, _ = await service._apply_supply_sale(
            None,  # type: ignore[arg-type]
            incoming(
                "supply_sale",
                "create",
                {
                    "sale_date": "2026-07-18",
                    "lines": [{"product_id": str(uuid4()), "quantity": "2.00"}],
                },
                offline_id,
            ),
        )

        assert inventory.calls[0][0] == "create"
        assert inventory.calls[0][1][0].id == offline_id
        assert version == 1
        assert data["total"] == "140.00"

    async def test_voiding_a_sale_carries_the_reason(self) -> None:
        inventory = FakeInventoryService()
        service = build_service(inventory=inventory)
        sale_id = uuid4()

        await service._apply_supply_sale(
            None,  # type: ignore[arg-type]
            incoming("supply_sale", "cancel", {"reason": "Cliente devolvió el bote"}, sale_id),
        )

        _, (target, cancellation, _) = inventory.calls[0]
        assert target == sale_id
        assert cancellation.reason == "Cliente devolvió el bote"


class FakeSyncRepository:
    def __init__(self) -> None:
        self.written: list[SyncOperation] = []

    async def get_operation(self, op_id: UUID) -> SyncOperation | None:
        del op_id
        return None

    def add(self, instance: Any) -> None:
        self.written.append(instance)

    async def commit(self) -> None:
        return None


class FakeIdentityService:
    def ensure_permission(self, user: Any, permission: str) -> None:
        del user, permission


class TestTheOutcomeOfARefusal:
    """Plan 0004 §8 and Plan 0005 §6.4: what the device is told, and why it matters.

    `conflict` and `rejected` land in the same review queue but are two different
    screens. A conflict shows both versions and asks which to keep; there is
    nothing to compare when the shelf is empty or the day is closed.
    """

    @staticmethod
    async def _outcome(failure: Exception) -> Any:
        service = build_service(
            repository=FakeSyncRepository(),
            identity=FakeIdentityService(),
            staff=FakeStaffService(fails_with=failure),
            inventory=FakeInventoryService(fails_with=failure),
        )
        device = SyncDevice(id=uuid4(), user_id=uuid4(), name="Tablet mostrador")
        user = SimpleNamespace(id=uuid4())
        entity, op_type, payload = (
            ("supply_sale", "create", {"lines": [{"product_id": str(uuid4()), "quantity": "2"}]})
            if isinstance(failure, InsufficientStock)
            else ("attendance_record", "update", {"clock_out": "13:00:00"})
        )
        return await service._apply_one(
            user,  # type: ignore[arg-type]
            device,
            incoming(entity, op_type, payload, uuid4(), base_version=1),
        )

    async def test_a_stale_version_is_the_only_conflict(self) -> None:
        result = await self._outcome(StaleVersionError("That working day changed."))

        assert result.status is OperationStatus.CONFLICT

    async def test_a_closed_day_is_a_rejection_and_not_a_conflict(self) -> None:
        """Nothing raced here: the day was shut on purpose, and the fix is to ask
        an administrator to reopen it — not to choose between two versions."""
        result = await self._outcome(ConflictError("Day 2026-07-18 is already closed."))

        assert result.status is OperationStatus.REJECTED
        assert "already closed" in (result.reason or "")

    async def test_an_empty_shelf_rejects_with_the_shelf_attached(self) -> None:
        """D12: the review queue has to show both sides, so the stock the server
        sees now travels with the refusal instead of only inside its sentence."""
        result = await self._outcome(
            InsufficientStock("Suavizante", Decimal("5.00"), Decimal("2.00"))
        )

        assert result.status is OperationStatus.REJECTED
        assert result.server_data == {
            "product_name": "Suavizante",
            "requested": "5.00",
            "available": "2.00",
        }


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
