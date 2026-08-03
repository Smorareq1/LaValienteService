from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError
from src.modules.catalog.repository import CatalogRepository
from src.modules.customers.service import CustomersService
from src.modules.identity.models import User
from src.modules.identity.service import IdentityService
from src.modules.orders.models import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_FROM,
    Order,
    OrderCharge,
    OrderDiscount,
    OrderGarment,
    OrderPayment,
    OrderStatus,
)
from src.modules.orders.pricing import (
    ChargeRequest,
    DiscountRequest,
    PriceBook,
    PricedOrder,
    price_order,
)
from src.modules.orders.repository import OrdersRepository
from src.modules.orders.schemas import (
    OrderCancel,
    OrderCreate,
    OrderDeliver,
    OrderPage,
    OrderPaymentCreate,
    OrderSummary,
)

#: Two devices taking a ticket in the same second both read the same `No.` and
#: one of them loses the insert (D11). Three tries is far more than the volume
#: here ever needs; the point is to fail loudly rather than to loop.
MAX_DAILY_NUMBER_ATTEMPTS = 3

DAILY_NUMBER_CONSTRAINT = "uq_orders_daily_number"
BOOKLET_CONSTRAINT = "uq_orders_booklet_serial"


def _new_payment(
    data: OrderPaymentCreate, *, received_by_id: UUID, is_advance: bool
) -> OrderPayment:
    """Build a payment row. `is_advance` is decided by the caller, not the body:
    it is a fact about *when* the money arrived, and the moment is the endpoint."""
    payment = OrderPayment(
        amount=data.amount,
        method=data.method,
        is_advance=is_advance,
        reference=data.reference,
        received_by_id=received_by_id,
        paid_at=datetime.now(UTC),
    )
    if data.id is not None:
        payment.id = data.id
    return payment


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Whether `error` is that constraint firing.

    Matched on the message because the driver-specific attribute that carries the
    constraint name (`asyncpg`'s `constraint_name`) is not part of the DBAPI, and
    the tests run against a different driver than production.
    """
    return constraint in str(error.orig)


class OrdersService:
    """Taking an order: price it, number it, write it down (Plan 0001 §6)."""

    def __init__(
        self,
        repository: OrdersRepository,
        catalog: CatalogRepository,
        customers: CustomersService,
        identity: IdentityService,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.customers = customers
        self.identity = identity

    async def get(self, order_id: UUID) -> Order:
        order = await self.repository.get(order_id)
        if order is None:
            raise NotFoundError("Order not found.")
        return order

    async def search(
        self,
        *,
        order_date: date | None = None,
        status: OrderStatus | None = None,
        customer_id: UUID | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrderPage:
        items, total = await self.repository.search(
            order_date=order_date,
            status=status,
            customer_id=customer_id,
            search=search,
            page=page,
            page_size=page_size,
        )
        return OrderPage(
            items=[OrderSummary.model_validate(order) for order in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create(self, data: OrderCreate, *, actor: User) -> tuple[Order, list[str]]:
        """Register a ticket and everything on it, in one transaction.

        Returns the order and the engine's warnings — things the counter should
        read but that must not stop a customer who is standing there waiting.
        """
        if data.discounts:
            # Checked here and not only at the endpoint because the condition is
            # the payload, not the route: an order without discounts is ordinary
            # counter work, one with them is an administrator's call.
            self.identity.ensure_permission(actor, "orders.manual_discount")

        order_date = data.order_date or business_date()
        if data.id is not None and await self.repository.get(data.id) is not None:
            raise ConflictError("An order with that id already exists.")

        await self._check_garments(data)
        customer_id = await self._resolve_customer(data)
        total_pieces = sum(garment.quantity for garment in data.garments)
        priced = await self._price(data, order_date, total_pieces)

        if data.advance_payment is not None and data.advance_payment.amount > priced.total:
            raise ConflictError("The advance is larger than what the order costs.")

        last_error: IntegrityError | None = None
        for _attempt in range(MAX_DAILY_NUMBER_ATTEMPTS):
            order = self._build(
                data,
                priced,
                order_date=order_date,
                daily_number=await self.repository.next_daily_number(order_date),
                customer_id=customer_id,
                total_pieces=total_pieces,
                received_by_id=actor.id,
            )
            try:
                async with self.repository.savepoint():
                    self.repository.add(order)
            except IntegrityError as error:
                if _violates(error, BOOKLET_CONSTRAINT):
                    raise ConflictError(
                        f"Booklet {data.booklet_serial} has already been registered."
                    ) from error
                if not _violates(error, DAILY_NUMBER_CONSTRAINT):
                    raise
                # Rolling back the savepoint leaves this order pending again, so
                # it has to go before the next attempt builds a replacement.
                self.repository.expunge(order)
                last_error = error
                continue

            await self.repository.commit()
            return order, priced.warnings

        raise ConflictError(
            "Could not assign a number for the day; please try again."
        ) from last_error

    async def _resolve_customer(self, data: OrderCreate) -> UUID:
        """Use the customer given, or register the new one along with the ticket."""
        if data.customer is not None:
            customer, _duplicate_of = await self.customers.stage(data.customer)
            return customer.id
        assert data.customer_id is not None  # the schema guarantees one of the two
        customer = await self.customers.get(data.customer_id)
        if not customer.is_active:
            raise ConflictError("That customer is archived and cannot take new orders.")
        return customer.id

    async def _check_garments(self, data: OrderCreate) -> None:
        """Reject unknown or repeated garment kinds before touching the database.

        A foreign-key violation would say the same thing in a language nobody at
        the counter can act on, and a repeated kind is a capture slip: two lines
        of "Camisa" mean the second overwrote the first in whoever's head.
        """
        seen: set[UUID] = set()
        for line in data.garments:
            if line.garment_type_id in seen:
                raise ConflictError("The same garment kind is listed twice.")
            seen.add(line.garment_type_id)

        if not seen:
            return
        known = {garment.id for garment in await self.catalog.list_garment_types()}
        unknown = seen - known
        if unknown:
            raise ConflictError(f"{len(unknown)} of the garment kinds do not exist.")

    async def _price(self, data: OrderCreate, order_date: date, total_pieces: int) -> PricedOrder:
        service_types = await self.catalog.list_service_types()
        prices = await self.catalog.list_prices_on(order_date)
        book = PriceBook(service_types, prices, order_date)

        return price_order(
            [
                ChargeRequest(
                    service_code=line.service_code,
                    option_code=line.option_code,
                    quantity=line.quantity,
                    amount=line.amount,
                )
                for line in data.charges
            ],
            [
                DiscountRequest(description=line.description, amount=line.amount)
                for line in data.discounts
            ],
            book,
            weight_lbs=data.weight_lbs,
            total_pieces=total_pieces,
        )

    @staticmethod
    def _build(
        data: OrderCreate,
        priced: PricedOrder,
        *,
        order_date: date,
        daily_number: int,
        customer_id: UUID,
        total_pieces: int,
        received_by_id: UUID,
    ) -> Order:
        order = Order(
            order_date=order_date,
            daily_number=daily_number,
            booklet_serial=data.booklet_serial,
            customer_id=customer_id,
            nit=data.nit,
            weight_lbs=data.weight_lbs,
            total_pieces=total_pieces,
            observations=data.observations,
            status=OrderStatus.RECEIVED,
            subtotal=priced.subtotal,
            discount_total=priced.discount_total,
            total=priced.total,
            received_by_id=received_by_id,
        )
        if data.id is not None:
            order.id = data.id

        order.charges = [
            OrderCharge(
                service_type_id=line.service_type_id,
                service_option_id=line.service_option_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for line in priced.charges
        ]
        order.discounts = [
            OrderDiscount(
                promotion_id=line.promotion_id,
                description=line.description,
                amount=line.amount,
            )
            for line in priced.discounts
        ]
        order.garments = [
            OrderGarment(
                garment_type_id=line.garment_type_id,
                quantity=line.quantity,
                notes=line.notes,
            )
            for line in data.garments
        ]
        # Assigned even when empty, like the other three collections: leaving it
        # untouched keeps it an unloaded lazy relationship, and the first read of
        # `balance` after the commit would go back to the database — which under
        # an async session is `MissingGreenlet`, not a slow query.
        advance = data.advance_payment
        order.payments = (
            []
            if advance is None
            else [_new_payment(advance, received_by_id=received_by_id, is_advance=True)]
        )
        return order

    # -- life cycle (§7) ---------------------------------------------------

    async def change_status(
        self, order_id: UUID, new_status: OrderStatus, *, actor: User
    ) -> Order:
        """Advance — or walk back — along the chain of §7.1.

        Takes `actor` it does not read, so that the four life-cycle methods share
        one signature: the sync applicator calls all of them the same way.
        """
        del actor
        order = await self.get(order_id)
        if new_status is order.status:
            # Two taps on the same button, or a device re-sending an operation it
            # never got the answer to. Nothing changed, so nothing is written.
            return order

        if new_status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
            raise ConflictError(
                f"Reaching '{new_status}' has an endpoint of its own: it needs data "
                "this one has no room for."
            )
        if new_status not in ALLOWED_TRANSITIONS[order.status]:
            raise ConflictError(f"An order that is '{order.status}' cannot become '{new_status}'.")

        order.status = new_status
        await self.repository.commit()
        return order

    async def deliver(self, order_id: UUID, data: OrderDeliver, *, actor: User) -> Order:
        """Hand the laundry back and close the ticket (§7.2)."""
        order = await self.get(order_id)
        if order.status is not OrderStatus.READY:
            raise ConflictError(
                f"Only an order that is 'ready' can be delivered; this one is '{order.status}'."
            )

        self._reconcile_garments(order, data)

        if data.payment is not None:
            self._take_payment(order, data.payment, actor=actor, is_advance=False)

        if order.balance > 0:
            # Lending is a decision, not an oversight: an administrator may hand
            # the clothes over with money still owed, and the ticket keeps the
            # debt visible afterwards.
            self.identity.ensure_permission(actor, "orders.deliver_unpaid")

        order.status = OrderStatus.DELIVERED
        order.delivered_at = datetime.now(UTC)
        order.delivered_by_id = actor.id
        await self.repository.commit()
        return order

    @staticmethod
    def _reconcile_garments(order: Order, data: OrderDeliver) -> None:
        """Write down what actually went back.

        An unlisted garment is assumed to go back whole: the counter only writes
        a number when something is off, and demanding the full list would make
        the ordinary delivery the tedious one.
        """
        declared = {line.garment_type_id: line.quantity_delivered for line in data.garments}
        on_ticket = {garment.garment_type_id for garment in order.garments}
        unknown = set(declared) - on_ticket
        if unknown:
            raise ConflictError(f"{len(unknown)} of those garment kinds are not on this order.")

        for garment in order.garments:
            returned = declared.get(garment.garment_type_id, garment.quantity)
            if returned > garment.quantity:
                raise ConflictError(
                    "More garments cannot be handed back than were received."
                )
            garment.quantity_delivered = returned

    async def cancel(self, order_id: UUID, data: OrderCancel, *, actor: User) -> Order:
        """Void a ticket, with the reason written down (§7.1)."""
        order = await self.get(order_id)
        if order.status not in CANCELLABLE_FROM:
            raise ConflictError(
                f"An order that is '{order.status}' can no longer be voided."
            )

        # Money already taken is left alone. Refunding is a cash movement of its
        # own and the till has to show it; silently erasing the payment would
        # leave the drawer short with nothing to point at.
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(UTC)
        order.cancelled_by_id = actor.id
        order.cancel_reason = data.reason
        await self.repository.commit()
        return order

    async def add_payment(
        self, order_id: UUID, data: OrderPaymentCreate, *, actor: User
    ) -> Order:
        """Register money received against a ticket.

        Delivered orders still accept payments: that is precisely how a ticket
        handed over on credit gets settled.
        """
        order = await self.get(order_id)
        if order.status is OrderStatus.CANCELLED:
            raise ConflictError("A voided order takes no payments.")

        self._take_payment(order, data, actor=actor, is_advance=data.is_advance)
        await self.repository.commit()
        return order

    @staticmethod
    def _take_payment(
        order: Order, data: OrderPaymentCreate, *, actor: User, is_advance: bool
    ) -> None:
        """Attach a payment, refusing to record more than is owed.

        Change handed back at the counter is not a payment: recording Q100 against
        a Q75 ticket would put twenty-five quetzales in the books that never
        stayed in the drawer.
        """
        if data.amount > order.balance:
            raise ConflictError(
                f"The payment is larger than the balance of {order.balance}."
            )
        if data.id is not None and any(payment.id == data.id for payment in order.payments):
            raise ConflictError("That payment is already registered.")

        order.payments.append(
            _new_payment(data, received_by_id=actor.id, is_advance=is_advance)
        )
