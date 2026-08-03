"""What may happen to a ticket after it is taken (Plan 0001 §7).

Capture is covered by `test_order_capture`; the arithmetic by `test_order_pricing`.
Here the questions are the ones the counter asks later: can this move forward, did
all the clothes go back, and who is allowed to hand them over with money still
owed.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import AuthorizationError, ConflictError
from src.modules.identity.models import User
from src.modules.orders.models import (
    Order,
    OrderGarment,
    OrderPayment,
    OrderStatus,
    PaymentMethod,
)
from src.modules.orders.schemas import (
    OrderCancel,
    OrderDeliver,
    OrderDeliverGarment,
    OrderPaymentCreate,
)
from src.modules.orders.service import OrdersService

ACTOR_ID = uuid4()
SHIRTS = uuid4()
SHEETS = uuid4()


def an_actor() -> User:
    """A real `User`, so the service is called the way the endpoint calls it."""
    user = User(username="mostrador", password_hash="x")
    user.id = ACTOR_ID
    return user


class FakeIdentityService:
    def __init__(self, permissions: set[str]) -> None:
        self.permissions = permissions

    def ensure_permission(self, _user: object, permission_code: str) -> None:
        if permission_code not in self.permissions:
            raise AuthorizationError("The account lacks the required permission.")


class FakeOrdersRepository:
    def __init__(self, order: Order) -> None:
        self.order = order
        self.commits = 0

    async def get(self, _order_id: UUID) -> Order:
        return self.order

    async def commit(self) -> None:
        self.commits += 1


def an_order(
    *,
    status: OrderStatus = OrderStatus.READY,
    total: Decimal = Decimal("100.00"),
    paid: Decimal | None = None,
) -> Order:
    order = Order(
        order_date=None,
        daily_number=1,
        customer_id=uuid4(),
        total_pieces=6,
        status=status,
        subtotal=total,
        discount_total=Decimal("0.00"),
        total=total,
        received_by_id=ACTOR_ID,
    )
    order.id = uuid4()
    order.garments = [
        OrderGarment(garment_type_id=SHIRTS, quantity=4),
        OrderGarment(garment_type_id=SHEETS, quantity=2),
    ]
    if paid is not None:
        order.payments = [
            OrderPayment(
                amount=paid,
                method=PaymentMethod.CASH,
                is_advance=True,
                received_by_id=ACTOR_ID,
            )
        ]
    return order


def build_service(
    order: Order, *, permissions: set[str] | None = None
) -> tuple[OrdersService, FakeOrdersRepository]:
    repository = FakeOrdersRepository(order)
    service = OrdersService(
        repository,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        FakeIdentityService(permissions or set()),  # type: ignore[arg-type]
    )
    return service, repository


class TestBalance:
    def test_a_ticket_with_no_payments_owes_all_of_it(self) -> None:
        assert an_order().balance == Decimal("100.00")

    def test_an_advance_comes_off_the_balance(self) -> None:
        order = an_order(paid=Decimal("30.00"))

        assert order.paid_total == Decimal("30.00")
        assert order.balance == Decimal("70.00")

    def test_a_voided_payment_stops_counting(self) -> None:
        """Payments are tombstoned, not deleted (Plan 0004 §6.1), so the balance
        has to read past the ones that were taken back."""
        from datetime import UTC, datetime

        order = an_order(paid=Decimal("30.00"))
        order.payments[0].deleted_at = datetime.now(UTC)

        assert order.balance == Decimal("100.00")


class TestStatus:
    async def test_it_moves_along_the_chain(self) -> None:
        order = an_order(status=OrderStatus.RECEIVED)
        service, repository = build_service(order)

        await service.change_status(order.id, OrderStatus.IN_PROGRESS, actor=an_actor())

        assert order.status is OrderStatus.IN_PROGRESS
        assert repository.commits == 1

    async def test_one_step_back_is_allowed(self) -> None:
        """§7.1: a ticket marked ready by mistake is undone at the counter."""
        order = an_order(status=OrderStatus.READY)
        service, _ = build_service(order)

        await service.change_status(order.id, OrderStatus.IN_PROGRESS, actor=an_actor())

        assert order.status is OrderStatus.IN_PROGRESS

    async def test_it_cannot_skip_a_step(self) -> None:
        order = an_order(status=OrderStatus.RECEIVED)
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="cannot become"):
            await service.change_status(order.id, OrderStatus.READY, actor=an_actor())

    async def test_delivery_is_not_reachable_from_here(self) -> None:
        """It would skip the garment reconciliation and the balance check."""
        order = an_order(status=OrderStatus.READY)
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="endpoint of its own"):
            await service.change_status(order.id, OrderStatus.DELIVERED, actor=an_actor())

    async def test_a_delivered_order_is_final(self) -> None:
        order = an_order(status=OrderStatus.DELIVERED)
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="cannot become"):
            await service.change_status(order.id, OrderStatus.IN_PROGRESS, actor=an_actor())

    async def test_asking_for_the_status_it_already_has_writes_nothing(self) -> None:
        """A device re-sending an operation it never got the answer to."""
        order = an_order(status=OrderStatus.READY)
        service, repository = build_service(order)

        await service.change_status(order.id, OrderStatus.READY, actor=an_actor())

        assert repository.commits == 0


class TestDelivery:
    async def test_a_settled_order_goes_back_whole(self) -> None:
        order = an_order(paid=Decimal("100.00"))
        service, repository = build_service(order)

        await service.deliver(order.id, OrderDeliver(), actor=an_actor())

        assert order.status is OrderStatus.DELIVERED
        assert order.delivered_by_id == ACTOR_ID
        assert order.delivered_at is not None
        # Unlisted garments are assumed to have gone back in full.
        assert [garment.quantity_delivered for garment in order.garments] == [4, 2]
        assert repository.commits == 1

    async def test_a_missing_garment_is_written_down(self) -> None:
        """The gap against what was received is the loss report (§5.3)."""
        order = an_order(paid=Decimal("100.00"))
        service, _ = build_service(order)

        one_shirt_short = OrderDeliverGarment(garment_type_id=SHIRTS, quantity_delivered=3)

        await service.deliver(
            order.id, OrderDeliver(garments=[one_shirt_short]), actor=an_actor()
        )

        assert order.garments[0].quantity_delivered == 3
        assert order.garments[1].quantity_delivered == 2

    async def test_more_cannot_go_back_than_came_in(self) -> None:
        order = an_order(paid=Decimal("100.00"))
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="More garments"):
            await service.deliver(
                order.id,
                OrderDeliver(
                    garments=[OrderDeliverGarment(garment_type_id=SHIRTS, quantity_delivered=9)]
                ),
                actor=an_actor(),
            )

    async def test_a_garment_that_is_not_on_the_ticket_is_refused(self) -> None:
        order = an_order(paid=Decimal("100.00"))
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="not on this order"):
            await service.deliver(
                order.id,
                OrderDeliver(
                    garments=[OrderDeliverGarment(garment_type_id=uuid4(), quantity_delivered=1)]
                ),
                actor=an_actor(),
            )

    async def test_only_a_ready_order_can_be_delivered(self) -> None:
        order = an_order(status=OrderStatus.IN_PROGRESS, paid=Decimal("100.00"))
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="'ready'"):
            await service.deliver(order.id, OrderDeliver(), actor=an_actor())

    async def test_the_payment_that_settles_it_rides_along(self) -> None:
        order = an_order(paid=Decimal("30.00"))
        service, _ = build_service(order)

        await service.deliver(
            order.id,
            OrderDeliver(payment=OrderPaymentCreate(amount=Decimal("70.00"))),
            actor=an_actor(),
        )

        assert order.balance == Decimal("0.00")
        assert order.status is OrderStatus.DELIVERED
        # The money at the counter is not an advance; the one taken at capture was.
        assert [payment.is_advance for payment in order.payments] == [True, False]

    async def test_handing_it_over_unpaid_takes_its_own_permission(self) -> None:
        """§7.2: lending is an administrator's call, not an oversight."""
        order = an_order()
        service, repository = build_service(order, permissions=set())

        with pytest.raises(AuthorizationError):
            await service.deliver(order.id, OrderDeliver(), actor=an_actor())
        assert order.status is OrderStatus.READY
        assert repository.commits == 0

    async def test_with_that_permission_the_debt_is_kept_visible(self) -> None:
        order = an_order()
        service, _ = build_service(order, permissions={"orders.deliver_unpaid"})

        await service.deliver(order.id, OrderDeliver(), actor=an_actor())

        assert order.status is OrderStatus.DELIVERED
        assert order.balance == Decimal("100.00")


class TestCancellation:
    async def test_it_records_who_and_why(self) -> None:
        order = an_order(status=OrderStatus.RECEIVED)
        service, repository = build_service(order)

        reason = OrderCancel(reason="Cliente se arrepintió")

        await service.cancel(order.id, reason, actor=an_actor())

        assert order.status is OrderStatus.CANCELLED
        assert order.cancel_reason == "Cliente se arrepintió"
        assert order.cancelled_by_id == ACTOR_ID
        assert order.cancelled_at is not None
        assert repository.commits == 1

    async def test_a_ready_order_can_no_longer_be_voided(self) -> None:
        """§7.1 only allows it from `received` or `in_progress`."""
        order = an_order(status=OrderStatus.READY)
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="no longer be voided"):
            await service.cancel(order.id, OrderCancel(reason="Ya no"), actor=an_actor())

    async def test_the_reason_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="at least 3"):
            OrderCancel(reason="")

    async def test_money_already_taken_is_left_alone(self) -> None:
        """Refunding is a cash movement of its own; erasing it would leave the
        till short with nothing to point at."""
        order = an_order(status=OrderStatus.RECEIVED, paid=Decimal("30.00"))
        service, _ = build_service(order)

        await service.cancel(order.id, OrderCancel(reason="Máquina descompuesta"), actor=an_actor())

        assert order.paid_total == Decimal("30.00")


class TestPayments:
    async def test_it_registers_the_money(self) -> None:
        order = an_order()
        service, repository = build_service(order)

        transfer = OrderPaymentCreate(
            amount=Decimal("40.00"), method=PaymentMethod.TRANSFER, reference="99812"
        )

        await service.add_payment(order.id, transfer, actor=an_actor())

        assert order.balance == Decimal("60.00")
        assert order.payments[0].method is PaymentMethod.TRANSFER
        assert order.payments[0].reference == "99812"
        assert order.payments[0].received_by_id == ACTOR_ID
        assert repository.commits == 1

    async def test_it_refuses_to_book_more_than_is_owed(self) -> None:
        """Change handed back at the counter is not a payment."""
        order = an_order()
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="larger than the balance"):
            await service.add_payment(
                order.id, OrderPaymentCreate(amount=Decimal("100.01")), actor=an_actor()
            )

    async def test_a_delivered_order_still_takes_payment(self) -> None:
        """That is how a ticket handed over on credit gets settled."""
        order = an_order(status=OrderStatus.DELIVERED)
        service, _ = build_service(order)

        await service.add_payment(
            order.id, OrderPaymentCreate(amount=Decimal("100.00")), actor=an_actor()
        )

        assert order.balance == Decimal("0.00")

    async def test_a_voided_order_does_not(self) -> None:
        order = an_order(status=OrderStatus.CANCELLED)
        service, _ = build_service(order)

        with pytest.raises(ConflictError, match="voided order"):
            await service.add_payment(
                order.id, OrderPaymentCreate(amount=Decimal("10.00")), actor=an_actor()
            )

    async def test_the_same_payment_id_is_not_booked_twice(self) -> None:
        """A device retrying a push it never got the answer to (Plan 0004 D4)."""
        order = an_order()
        service, _ = build_service(order)
        payment_id = uuid4()

        await service.add_payment(
            order.id, OrderPaymentCreate(amount=Decimal("40.00"), id=payment_id), actor=an_actor()
        )
        with pytest.raises(ConflictError, match="already registered"):
            await service.add_payment(
                order.id,
                OrderPaymentCreate(amount=Decimal("40.00"), id=payment_id),
                actor=an_actor(),
            )

        assert order.balance == Decimal("60.00")
