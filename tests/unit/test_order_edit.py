"""Correcting a ticket and adding up the day (Plan 0001 §7.3 and §8).

The question here is not the arithmetic — that is `test_order_pricing` — but who
may rewrite a boleta, when, and what a rewrite is not allowed to undo.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import AuthorizationError, ConflictError
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.customers.models import Customer
from src.modules.orders.models import (
    Order,
    OrderCharge,
    OrderGarment,
    OrderPayment,
    OrderStatus,
    PaymentMethod,
)
from src.modules.orders.repository import DayStatusTotals
from src.modules.orders.schemas import (
    OrderChargeCreate,
    OrderDiscountCreate,
    OrderGarmentCreate,
    OrderUpdate,
)
from src.modules.orders.service import OrdersService
from src.modules.promotions.models import Promotion

ORDER_DATE = date(2026, 7, 20)
ACTOR_ID = uuid4()
CUSTOMER_ID = uuid4()
SHIRTS = uuid4()


class FakeUser:
    id = ACTOR_ID


def _build_catalog() -> tuple[list[ServiceType], list[ServicePrice]]:
    tub = ServiceType(code="wash_tub", name="Lavado por tina", pricing_mode=PricingMode.TIERED)
    tub.id = uuid4()
    big = ServiceOption(code="G", name="Tina grande")
    big.id = uuid4()
    big.is_active = True
    big.deleted_at = None
    small = ServiceOption(code="P", name="Tina pequeña")
    small.id = uuid4()
    small.is_active = True
    small.deleted_at = None
    tub.options = [big, small]

    return [tub], [
        ServicePrice(
            service_type_id=tub.id,
            service_option_id=big.id,
            price=Decimal("30.00"),
            valid_from=date(2026, 1, 1),
            valid_to=None,
        ),
        ServicePrice(
            service_type_id=tub.id,
            service_option_id=small.id,
            price=Decimal("20.00"),
            valid_from=date(2026, 1, 1),
            valid_to=None,
        ),
    ]


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
        return [FakeGarmentType(SHIRTS)]


class FakePromotionsRepository:
    async def list_all(self, **_: object) -> list[Promotion]:
        return []


class FakeCustomersService:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active

    async def get(self, customer_id: UUID) -> Customer:
        customer = Customer(full_name="Marta González")
        customer.id = customer_id
        customer.is_active = self.active
        return customer


class FakeIdentityService:
    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions

    def ensure_permission(self, _user: object, permission_code: str) -> None:
        if permission_code not in self.permissions:
            raise AuthorizationError("The account lacks the required permission.")


class FakeOrdersRepository:
    def __init__(self, order: Order | None = None) -> None:
        self.order = order
        self.commits = 0
        self.flushes = 0
        self.day: tuple[list[DayStatusTotals], dict[OrderStatus, Decimal]] = ([], {})

    async def get(self, _order_id: UUID, *, refresh: bool = False) -> Order | None:
        order = self.order
        if refresh and order is not None:
            # Lo que hace la relectura con `populate_existing`: dejar fuera las
            # líneas que la corrección acaba de marcar como borradas.
            order.charges = [line for line in order.charges if line.deleted_at is None]
            order.discounts = [line for line in order.discounts if line.deleted_at is None]
            order.garments = [line for line in order.garments if line.deleted_at is None]
        return order

    async def summarize_day(
        self, _order_date: date
    ) -> tuple[list[DayStatusTotals], dict[OrderStatus, Decimal]]:
        return self.day

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


def an_order(
    *,
    status: OrderStatus = OrderStatus.RECEIVED,
    paid: Decimal | None = None,
    version: int = 1,
) -> Order:
    """A ticket of one large tub, as the counter took it."""
    order = Order(
        order_date=ORDER_DATE,
        daily_number=7,
        customer_id=CUSTOMER_ID,
        total_pieces=4,
        status=status,
        subtotal=Decimal("30.00"),
        discount_total=Decimal("0.00"),
        total=Decimal("30.00"),
        received_by_id=ACTOR_ID,
    )
    order.id = uuid4()
    order.version = version
    order.garments = [OrderGarment(garment_type_id=SHIRTS, quantity=4)]
    order.charges = [
        OrderCharge(
            service_type_id=SERVICE_TYPES[0].id,
            service_option_id=SERVICE_TYPES[0].options[0].id,
            description="Lavado por tina — Tina grande",
            quantity=Decimal(1),
            unit_price=Decimal("30.00"),
            amount=Decimal("30.00"),
        )
    ]
    order.discounts = []
    order.payments = (
        []
        if paid is None
        else [
            OrderPayment(
                amount=paid,
                method=PaymentMethod.CASH,
                is_advance=True,
                received_by_id=ACTOR_ID,
            )
        ]
    )
    return order


def build_service(
    order: Order | None = None,
    *,
    permissions: set[str] | None = None,
    customer_active: bool = True,
) -> tuple[OrdersService, FakeOrdersRepository]:
    repository = FakeOrdersRepository(order)
    service = OrdersService(
        repository,  # type: ignore[arg-type]
        FakeCatalogRepository(),  # type: ignore[arg-type]
        FakePromotionsRepository(),  # type: ignore[arg-type]
        FakeCustomersService(active=customer_active),  # type: ignore[arg-type]
        FakeIdentityService(permissions or set()),  # type: ignore[arg-type]
    )
    return service, repository


def an_edit(**overrides: object) -> OrderUpdate:
    payload: dict[str, object] = {
        "charges": [OrderChargeCreate(service_code="wash_tub", option_code="P")],
        "garments": [OrderGarmentCreate(garment_type_id=SHIRTS, quantity=3)],
    }
    payload.update(overrides)
    return OrderUpdate(**payload)


class TestEditRules:
    """§7.3: the table of who may edit what, and when."""

    @pytest.mark.parametrize("status", [OrderStatus.RECEIVED, OrderStatus.IN_PROGRESS])
    async def test_a_ticket_still_in_the_shop_is_edited_by_anyone_who_takes_them(
        self, status: OrderStatus
    ) -> None:
        service, repository = build_service(an_order(status=status))

        order, _ = await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.total == Decimal("20.00")
        assert repository.commits == 1

    async def test_editing_a_ready_ticket_needs_the_extra_permission(self) -> None:
        """It has been counted, washed and folded: rewriting it is an admin's call."""
        service, repository = build_service(an_order(status=OrderStatus.READY))

        with pytest.raises(AuthorizationError):
            await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]
        assert repository.commits == 0

    async def test_an_admin_may_edit_a_ready_ticket(self) -> None:
        service, _ = build_service(
            an_order(status=OrderStatus.READY), permissions={"orders.update_ready"}
        )

        order, _ = await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.total == Decimal("20.00")

    @pytest.mark.parametrize("status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED])
    async def test_a_closed_ticket_is_not_editable_by_anyone(
        self, status: OrderStatus
    ) -> None:
        """Correcting one of these means voiding it and taking it again."""
        service, _ = build_service(
            an_order(status=status), permissions={"orders.update_ready", "*.*"}
        )

        with pytest.raises(ConflictError, match="no longer be edited"):
            await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]


class TestEdit:
    async def test_the_ticket_is_replaced_whole_and_priced_again(self) -> None:
        service, _ = build_service(an_order())

        order, _ = await service.update(
            uuid4(),
            an_edit(
                charges=[
                    OrderChargeCreate(service_code="wash_tub", option_code="G"),
                    OrderChargeCreate(service_code="wash_tub", option_code="P"),
                ],
                observations="Sin suavizante",
            ),
            actor=FakeUser(),  # type: ignore[arg-type]
        )

        assert [line.amount for line in order.charges] == [Decimal("30.00"), Decimal("20.00")]
        assert order.subtotal == Decimal("50.00")
        assert order.total == Decimal("50.00")
        assert order.total_pieces == 3
        assert order.observations == "Sin suavizante"

    async def test_the_date_and_the_number_are_not_editable(self) -> None:
        """They are what everyone calls the ticket by, and they belong to the day."""
        service, _ = build_service(an_order())

        order, _ = await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.order_date == ORDER_DATE
        assert order.daily_number == 7
        assert "order_date" not in OrderUpdate.model_fields
        assert "status" not in OrderUpdate.model_fields

    async def test_the_payments_are_left_alone(self) -> None:
        service, _ = build_service(an_order(paid=Decimal("10.00")))

        order, _ = await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]

        assert order.paid_total == Decimal("10.00")
        assert order.balance == Decimal("10.00")

    async def test_an_edit_that_drops_below_what_was_paid_is_a_refund(self) -> None:
        """And a refund is a cash movement this module cannot record (§7.3)."""
        service, repository = build_service(an_order(paid=Decimal("25.00")))

        with pytest.raises(ConflictError, match="already has"):
            await service.update(uuid4(), an_edit(), actor=FakeUser())  # type: ignore[arg-type]
        assert repository.commits == 0

    async def test_a_manual_discount_still_demands_its_permission(self) -> None:
        service, _ = build_service(an_order())

        with pytest.raises(AuthorizationError):
            await service.update(
                uuid4(),
                an_edit(
                    discounts=[OrderDiscountCreate(description="Cortesía", amount=Decimal("5.00"))]
                ),
                actor=FakeUser(),  # type: ignore[arg-type]
            )

    async def test_the_same_garment_kind_twice_is_still_a_capture_slip(self) -> None:
        service, _ = build_service(an_order())

        with pytest.raises(ConflictError, match="twice"):
            await service.update(
                uuid4(),
                an_edit(
                    garments=[
                        OrderGarmentCreate(garment_type_id=SHIRTS, quantity=2),
                        OrderGarmentCreate(garment_type_id=SHIRTS, quantity=1),
                    ]
                ),
                actor=FakeUser(),  # type: ignore[arg-type]
            )

    async def test_moving_the_ticket_to_an_archived_customer_is_refused(self) -> None:
        service, _ = build_service(an_order(), customer_active=False)

        with pytest.raises(ConflictError, match="archived"):
            await service.update(
                uuid4(), an_edit(customer_id=uuid4()), actor=FakeUser()  # type: ignore[arg-type]
            )

    async def test_a_stale_base_version_is_a_conflict_not_an_overwrite(self) -> None:
        """Plan 0004 D6: the ticket changed while the device was away."""
        service, repository = build_service(an_order(version=3))

        with pytest.raises(ConflictError, match="changed since"):
            await service.update(uuid4(), an_edit(base_version=2), actor=FakeUser())  # type: ignore[arg-type]
        assert repository.commits == 0

    async def test_the_matching_base_version_goes_through(self) -> None:
        service, _ = build_service(an_order(version=3))

        order, _ = await service.update(uuid4(), an_edit(base_version=3), actor=FakeUser())  # type: ignore[arg-type]

        assert order.total == Decimal("20.00")


class TestDailySummary:
    """§8: the day added up — the seed of the daily close."""

    @staticmethod
    def _day() -> tuple[list[DayStatusTotals], dict[OrderStatus, Decimal]]:
        return (
            [
                DayStatusTotals(
                    status=OrderStatus.DELIVERED,
                    orders=2,
                    pieces=10,
                    subtotal=Decimal("100.00"),
                    discount_total=Decimal("10.00"),
                    total=Decimal("90.00"),
                ),
                DayStatusTotals(
                    status=OrderStatus.RECEIVED,
                    orders=1,
                    pieces=4,
                    subtotal=Decimal("30.00"),
                    discount_total=Decimal("0.00"),
                    total=Decimal("30.00"),
                ),
                DayStatusTotals(
                    status=OrderStatus.CANCELLED,
                    orders=1,
                    pieces=6,
                    subtotal=Decimal("50.00"),
                    discount_total=Decimal("0.00"),
                    total=Decimal("50.00"),
                ),
            ],
            {
                OrderStatus.DELIVERED: Decimal("90.00"),
                OrderStatus.CANCELLED: Decimal("20.00"),
            },
        )

    async def test_a_voided_ticket_counts_as_a_ticket_but_not_as_money(self) -> None:
        service, repository = build_service()
        repository.day = self._day()

        summary = await service.daily_summary(ORDER_DATE)

        assert summary.orders == 4
        assert summary.by_status[OrderStatus.CANCELLED] == 1
        # The Q50 of the voided ticket is in none of the three money figures.
        assert summary.subtotal == Decimal("130.00")
        assert summary.discount_total == Decimal("10.00")
        assert summary.total == Decimal("120.00")
        assert summary.pieces == 14

    async def test_money_taken_on_a_voided_ticket_is_still_in_the_drawer(self) -> None:
        """Voiding does not touch what was already collected (§7.3), so the Q20
        is reported as collected — and the balance ignores it, because there is
        no live ticket left for it to settle."""
        service, repository = build_service()
        repository.day = self._day()

        summary = await service.daily_summary(ORDER_DATE)

        assert summary.collected == Decimal("110.00")
        assert summary.balance == Decimal("30.00")

    async def test_a_day_with_no_orders_answers_in_zeros(self) -> None:
        service, _ = build_service()

        summary = await service.daily_summary(ORDER_DATE)

        assert summary.orders == 0
        assert summary.by_status == {}
        assert summary.total == Decimal("0.00")
        assert summary.balance == Decimal("0.00")
