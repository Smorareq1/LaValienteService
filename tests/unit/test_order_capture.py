"""Rules of taking an order that live above the arithmetic (Plan 0001 §6, D11).

The engine's own tests are in `test_order_pricing`. What is exercised here is
what the service adds around it: who may discount, how the daily correlative
survives a collision, and what the ticket ends up carrying.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import AuthorizationError, ConflictError
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.customers.models import Customer
from src.modules.customers.schemas import CustomerCreate
from src.modules.orders.models import Order, OrderStatus
from src.modules.orders.schemas import (
    OrderChargeCreate,
    OrderCreate,
    OrderDiscountCreate,
    OrderGarmentCreate,
    OrderPaymentCreate,
)
from src.modules.orders.service import DAILY_NUMBER_CONSTRAINT, OrdersService

ORDER_DATE = date(2026, 7, 20)
GARMENT_ID = uuid4()
ACTOR_ID = uuid4()
CUSTOMER_ID = uuid4()


class FakeUser:
    id = ACTOR_ID


def _build_catalog() -> tuple[list[ServiceType], list[ServicePrice]]:
    tub = ServiceType(code="wash_tub", name="Lavado por tina", pricing_mode=PricingMode.TIERED)
    tub.id = uuid4()
    option = ServiceOption(code="G", name="Tina grande")
    option.id = uuid4()
    option.is_active = True
    option.deleted_at = None
    tub.options = [option]

    price = ServicePrice(
        service_type_id=tub.id,
        service_option_id=option.id,
        price=Decimal("30.00"),
        valid_from=date(2026, 1, 1),
        valid_to=None,
    )
    return [tub], [price]


#: Built once: the prices point at the service by id, so handing out a fresh copy
#: per call would leave every line unpriced.
SERVICE_TYPES, SERVICE_PRICES = _build_catalog()


class FakeGarmentType:
    def __init__(self, garment_id: UUID) -> None:
        self.id = garment_id


class FakeCatalogRepository:
    async def list_service_types(self, **_: object) -> list[ServiceType]:
        return SERVICE_TYPES

    async def list_prices_on(self, *_: object, **__: object) -> list[ServicePrice]:
        return SERVICE_PRICES

    async def list_garment_types(self, **_: object) -> list[FakeGarmentType]:
        return [FakeGarmentType(GARMENT_ID)]


class FakeCustomersService:
    def __init__(self) -> None:
        self.staged: list[object] = []

    async def get(self, customer_id: UUID) -> Customer:
        customer = Customer(full_name="Marta González")
        customer.id = customer_id
        customer.is_active = True
        return customer

    async def stage(self, data: object) -> tuple[Customer, None]:
        customer = Customer(full_name="Nuevo")
        customer.id = uuid4()
        self.staged.append(data)
        return customer, None


class FakeIdentityService:
    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions

    def ensure_permission(self, _user: object, permission_code: str) -> None:
        if permission_code not in self.permissions:
            raise AuthorizationError("The account lacks the required permission.")


def _integrity_error(constraint: str) -> IntegrityError:
    return IntegrityError(
        statement="INSERT INTO orders …",
        params=None,
        orig=Exception(f'duplicate key value violates unique constraint "{constraint}"'),
    )


class FakeOrdersRepository:
    """Records what the service does; can be told to reject the first insert."""

    def __init__(self, *, collide_times: int = 0) -> None:
        self.collide_times = collide_times
        self.next_number = 1
        self.added: list[Order] = []
        self.expunged: list[Order] = []
        self.commits = 0

    async def get(self, _order_id: UUID) -> Order | None:
        return None

    async def next_daily_number(self, _order_date: date) -> int:
        return self.next_number

    def add(self, instance: Order) -> None:
        self.added.append(instance)

    def expunge(self, instance: Order) -> None:
        self.expunged.append(instance)

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        yield
        if self.collide_times > 0:
            self.collide_times -= 1
            # What the database does when someone else took the number first.
            self.next_number += 1
            raise _integrity_error(DAILY_NUMBER_CONSTRAINT)

    async def commit(self) -> None:
        self.commits += 1


def build_service(
    *, permissions: set[str] | None = None, collide_times: int = 0
) -> tuple[OrdersService, FakeOrdersRepository]:
    repository = FakeOrdersRepository(collide_times=collide_times)
    service = OrdersService(
        repository,  # type: ignore[arg-type]
        FakeCatalogRepository(),  # type: ignore[arg-type]
        FakeCustomersService(),  # type: ignore[arg-type]
        FakeIdentityService(permissions or set()),  # type: ignore[arg-type]
    )
    return service, repository


def a_ticket(**overrides: object) -> OrderCreate:
    payload: dict[str, object] = {
        "order_date": ORDER_DATE,
        "customer_id": CUSTOMER_ID,
        "charges": [OrderChargeCreate(service_code="wash_tub", option_code="G")],
    }
    payload.update(overrides)
    return OrderCreate(**payload)


class TestCapture:
    async def test_a_plain_ticket_is_priced_numbered_and_received(self) -> None:
        service, repository = build_service()

        order, warnings = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.daily_number == 1
        assert order.status is OrderStatus.RECEIVED
        assert order.total == Decimal("30.00")
        assert order.received_by_id == ACTOR_ID
        assert warnings == []
        assert repository.commits == 1

    async def test_total_pieces_is_computed_not_taken(self) -> None:
        """§6.3: the paper demanded the counts agree; the system guarantees it."""
        service, _ = build_service()

        order, _ = await service.create(
            a_ticket(
                garments=[
                    OrderGarmentCreate(garment_type_id=GARMENT_ID, quantity=8),
                ]
            ),
            actor=FakeUser(),  # type: ignore[arg-type]
        )

        assert order.total_pieces == 8
        assert len(order.garments) == 1

    async def test_the_same_garment_kind_twice_is_a_capture_slip(self) -> None:
        service, _ = build_service()

        with pytest.raises(ConflictError, match="twice"):
            await service.create(
                a_ticket(
                    garments=[
                        OrderGarmentCreate(garment_type_id=GARMENT_ID, quantity=8),
                        OrderGarmentCreate(garment_type_id=GARMENT_ID, quantity=1),
                    ]
                ),
                actor=FakeUser(),  # type: ignore[arg-type]
            )

    async def test_an_unknown_garment_kind_is_refused_before_the_insert(self) -> None:
        service, repository = build_service()

        with pytest.raises(ConflictError, match="do not exist"):
            await service.create(
                a_ticket(garments=[OrderGarmentCreate(garment_type_id=uuid4(), quantity=1)]),
                actor=FakeUser(),  # type: ignore[arg-type]
            )
        assert repository.added == []

    async def test_a_fresh_ticket_can_report_its_balance(self) -> None:
        """The four collections are assigned even when empty.

        Leaving `payments` untouched keeps it an unloaded lazy relationship, and
        reading `balance` right after the commit — which the response schema
        does — goes back to the database. Under an async session that is not a
        slow query but `MissingGreenlet`.
        """
        service, _ = build_service()

        order, _ = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert "payments" not in inspect(order).unloaded
        assert order.balance == Decimal("30.00")

    async def test_an_advance_comes_off_the_balance_from_the_start(self) -> None:
        """§6.1: money taken at the counter, before the work is done."""
        service, _ = build_service()

        order, _ = await service.create(
            a_ticket(advance_payment=OrderPaymentCreate(amount=Decimal("10.00"))),
            actor=FakeUser(),  # type: ignore[arg-type]
        )

        assert order.payments[0].is_advance is True
        assert order.balance == Decimal("20.00")

    async def test_an_advance_larger_than_the_ticket_is_refused(self) -> None:
        service, repository = build_service()

        with pytest.raises(ConflictError, match="advance is larger"):
            await service.create(
                a_ticket(advance_payment=OrderPaymentCreate(amount=Decimal("500.00"))),
                actor=FakeUser(),  # type: ignore[arg-type]
            )
        assert repository.added == []

    async def test_charge_snapshots_carry_the_price_of_the_day(self) -> None:
        """D2: the ticket is a document, not a view over today's catalog."""
        service, _ = build_service()

        order, _ = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert len(order.charges) == 1
        assert order.charges[0].unit_price == Decimal("30.00")
        assert order.charges[0].description == "Lavado por tina — Tina grande"


class TestManualDiscount:
    async def test_a_discount_demands_its_own_permission(self) -> None:
        """§6.2 step 3: any collaborator may take an order, not discount it."""
        service, repository = build_service(permissions=set())

        courtesy = OrderDiscountCreate(description="Cortesía", amount=Decimal(5))

        with pytest.raises(AuthorizationError):
            await service.create(a_ticket(discounts=[courtesy]), actor=FakeUser())  # type: ignore[arg-type]
        assert repository.added == []

    async def test_with_the_permission_it_goes_through(self) -> None:
        service, _ = build_service(permissions={"orders.manual_discount"})

        order, _ = await service.create(
            a_ticket(discounts=[OrderDiscountCreate(description="Cortesía", amount=Decimal(5))]),
            actor=FakeUser(),  # type: ignore[arg-type]
        )

        assert order.discount_total == Decimal("5.00")
        assert order.total == Decimal("25.00")

    async def test_a_ticket_with_no_discount_needs_no_permission(self) -> None:
        """The check hangs off the payload, so it must not fire on ordinary work."""
        service, _ = build_service(permissions=set())

        order, _ = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.discounts == []


class TestDailyNumberCollision:
    async def test_a_taken_number_is_retried_with_the_next_one(self) -> None:
        """D11: two devices reading the same `No.` in the same second."""
        service, repository = build_service(collide_times=1)

        order, _ = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.daily_number == 2
        assert repository.commits == 1

    async def test_the_rejected_order_is_dropped_before_the_retry(self) -> None:
        """Rolling back the savepoint leaves it pending; two would be inserted."""
        service, repository = build_service(collide_times=1)

        order, _ = await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]

        assert len(repository.expunged) == 1
        assert repository.expunged[0] is not order
        assert repository.added[-1] is order

    async def test_it_gives_up_instead_of_looping(self) -> None:
        service, repository = build_service(collide_times=99)

        with pytest.raises(ConflictError, match="number for the day"):
            await service.create(a_ticket(), actor=FakeUser())  # type: ignore[arg-type]
        assert repository.commits == 0
        assert len(repository.added) == 3

    async def test_a_repeated_booklet_is_not_retried(self) -> None:
        """A different ticket carrying the same printed serial is a mistake, and
        trying again would only make the same one."""
        service, repository = build_service()
        repository.collide_times = 1

        async def booklet_collision() -> None:
            raise _integrity_error("uq_orders_booklet_serial")

        @asynccontextmanager
        async def savepoint() -> AsyncIterator[None]:
            yield
            await booklet_collision()

        repository.savepoint = savepoint  # type: ignore[method-assign]

        with pytest.raises(ConflictError, match="045213"):
            await service.create(a_ticket(booklet_serial="045213"), actor=FakeUser())  # type: ignore[arg-type]


class TestCustomerOnTheFly:
    async def test_an_unknown_customer_is_registered_with_the_ticket(self) -> None:
        service, _ = build_service()

        order, _ = await service.create(
            OrderCreate(
                order_date=ORDER_DATE,
                customer=CustomerCreate(full_name="Nuevo Cliente", phone="4815-2964"),
                charges=[OrderChargeCreate(service_code="wash_tub", option_code="G")],
            ),
            actor=FakeUser(),  # type: ignore[arg-type]
        )

        assert order.customer_id is not None

    async def test_exactly_one_of_the_two_has_to_come(self) -> None:
        with pytest.raises(ValueError, match="not both and not neither"):
            OrderCreate(
                customer_id=CUSTOMER_ID,
                customer=CustomerCreate(full_name="Nuevo"),
                charges=[OrderChargeCreate(service_code="wash_tub", option_code="G")],
            )

        with pytest.raises(ValueError, match="not both and not neither"):
            OrderCreate(charges=[OrderChargeCreate(service_code="wash_tub", option_code="G")])
