from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.core.mixins import SyncableMixin
from src.modules.catalog.repository import CatalogRepository
from src.modules.customers.service import CustomersService
from src.modules.daily_close.lock import ClosedDays, ensure_open
from src.modules.daily_close.totals import Split
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
    ManualDiscount,
    PriceBook,
    PricedOrder,
    PromotionBook,
    PromotionDiscount,
    price_order,
)
from src.modules.orders.repository import OrdersRepository
from src.modules.orders.schemas import (
    DailySummary,
    OrderCancel,
    OrderCreate,
    OrderDeliver,
    OrderDiscountCreate,
    OrderPage,
    OrderPaymentCreate,
    OrderSummary,
    OrderUpdate,
)
from src.modules.promotions.repository import PromotionsRepository

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


def _discount_request(line: OrderDiscountCreate) -> DiscountRequest:
    """Turn a captured discount into what the engine takes.

    The schema already guaranteed it is one shape or the other; this only picks
    which.
    """
    if line.promotion_code is not None:
        return PromotionDiscount(promotion_code=line.promotion_code)
    assert line.description is not None and line.amount is not None
    return ManualDiscount(description=line.description, amount=line.amount)


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
        promotions: PromotionsRepository,
        customers: CustomersService,
        identity: IdentityService,
        closed_days: ClosedDays | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.promotions = promotions
        self.customers = customers
        self.identity = identity
        #: The date lock of D9. Optional so a unit test can build the service
        #: without a close to consult; every wired path passes one.
        self.closed_days = closed_days

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
        if any(discount.is_manual for discount in data.discounts):
            # Only the manual ones. Picking a promotion is ordinary counter work
            # — the discount was decided in advance and the engine checks it is
            # in force — while typing an amount is an administrator's call (D10).
            # Checked here and not at the endpoint because the condition is the
            # payload, not the route.
            self.identity.ensure_permission(actor, "orders.manual_discount")

        order_date = data.order_date or business_date()
        await ensure_open(self.closed_days, order_date)
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

    async def _check_garments(self, data: OrderCreate | OrderUpdate) -> None:
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

    async def _price(
        self, data: OrderCreate | OrderUpdate, order_date: date, total_pieces: int
    ) -> PricedOrder:
        service_types = await self.catalog.list_service_types()
        prices = await self.catalog.list_prices_on(order_date)
        book = PriceBook(service_types, prices, order_date)
        # Every promotion, live or not, resolved against the *order's* date and
        # not today's: a ticket captured offline while a promotion was running
        # can reach the server after it ended, and "esa promoción ya venció" is
        # the answer the counter needs — not "no existe" (Plan 0004 §8). The
        # table holds a handful of rows, so reading all of them costs nothing.
        promotions = PromotionBook(
            await self.promotions.list_all(include_inactive=True), order_date
        )

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
            [_discount_request(line) for line in data.discounts],
            book,
            promotions=promotions,
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

    # -- editing a ticket that is still in the shop (§7.3) ------------------

    async def update(
        self, order_id: UUID, data: OrderUpdate, *, actor: User
    ) -> tuple[Order, list[str]]:
        """Rewrite a ticket and price it again from scratch.

        The ticket is **replaced**, not patched: what arrives is how the boleta
        should read, and the total is recomputed against the catalog of the
        order's own date — not today's — so correcting a ticket from Monday does
        not silently reprice it at Wednesday's rates (D1).
        """
        order = await self.get(order_id)
        self._ensure_editable(order, actor=actor)
        # Rewriting the ticket changes what its day recorded, so the lock is on
        # the ticket's own date here (D9) — unlike delivering it, which is a
        # thing that happens today.
        await ensure_open(self.closed_days, order.order_date)

        if data.base_version is not None and data.base_version != order.version:
            # Plan 0004 D6: someone else changed this ticket while the device was
            # away. Merging two versions of a boleta is guesswork; the operation
            # goes to the review queue with both in hand.
            raise StaleVersionError(
                f"This order changed since you last saw it (version {order.version})."
            )

        if any(discount.is_manual for discount in data.discounts):
            self.identity.ensure_permission(actor, "orders.manual_discount")

        await self._check_garments(data)
        customer_id = order.customer_id
        if data.customer_id is not None and data.customer_id != order.customer_id:
            customer = await self.customers.get(data.customer_id)
            if not customer.is_active:
                raise ConflictError("That customer is archived and cannot take new orders.")
            customer_id = customer.id

        total_pieces = sum(garment.quantity for garment in data.garments)
        priced = await self._price(data, order.order_date, total_pieces)

        if priced.total < order.paid_total:
            # The ticket would end up costing less than has already been paid,
            # which is a refund and not an edit — and refunds are a cash movement
            # this module has no way to record (§7.3).
            raise ConflictError(
                f"The order already has {order.paid_total} paid, more than the new "
                f"total of {priced.total}."
            )

        order.booklet_serial = data.booklet_serial
        order.customer_id = customer_id
        order.nit = data.nit
        order.weight_lbs = data.weight_lbs
        order.observations = data.observations
        order.total_pieces = total_pieces
        order.subtotal = priced.subtotal
        order.discount_total = priced.discount_total
        order.total = priced.total

        # The old lines become tombstones instead of disappearing (D8): a device
        # that already pulled them has to learn they are gone, and an erased row
        # travels down no feed. They are marked and flushed *before* the new ones
        # go in, so a ticket that keeps the same garment kind does not collide
        # with the very row on its way out.
        replaced = datetime.now(UTC)
        old_lines: list[SyncableMixin] = [*order.charges, *order.discounts, *order.garments]
        for line in old_lines:
            line.deleted_at = replaced
        await self.repository.flush()

        order.charges.extend(
            OrderCharge(
                service_type_id=line.service_type_id,
                service_option_id=line.service_option_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for line in priced.charges
        )
        order.discounts.extend(
            OrderDiscount(
                promotion_id=line.promotion_id,
                description=line.description,
                amount=line.amount,
            )
            for line in priced.discounts
        )
        order.garments.extend(
            OrderGarment(
                garment_type_id=line.garment_type_id,
                quantity=line.quantity,
                notes=line.notes,
            )
            for line in data.garments
        )

        try:
            await self.repository.commit()
        except IntegrityError as error:
            if _violates(error, BOOKLET_CONSTRAINT):
                raise ConflictError(
                    f"Booklet {data.booklet_serial} has already been registered."
                ) from error
            raise

        # Re-read so the answer carries the ticket as it now stands: the lines
        # just tombstoned are still in the session's collections.
        fresh = await self.repository.get(order_id, refresh=True)
        return fresh or order, priced.warnings

    def _ensure_editable(self, order: Order, *, actor: User) -> None:
        """The table of §7.3, which is about *who* may edit *when*.

        A ticket that is already `ready` has been counted, washed and folded;
        rewriting what it says at that point is an administrator's call, and the
        counter's way to fix one is to void it and take it again.
        """
        if order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
            raise ConflictError(
                f"An order that is '{order.status}' can no longer be edited: "
                "correcting one means voiding it and taking it again."
            )
        if order.status is OrderStatus.READY:
            self.identity.ensure_permission(actor, "orders.update_ready")

    # -- the day added up (§8) ---------------------------------------------

    async def daily_summary(self, order_date: date) -> DailySummary:
        """Count and add up the day's tickets — the seed of the daily close."""
        totals, paid = await self.repository.summarize_day(order_date)

        live = [row for row in totals if row.status is not OrderStatus.CANCELLED]
        collected_on_live = sum(
            (amount for status, amount in paid.items() if status is not OrderStatus.CANCELLED),
            Decimal("0.00"),
        )
        total = sum((row.total for row in live), Decimal("0.00"))

        return DailySummary(
            order_date=order_date,
            orders=sum(row.orders for row in totals),
            by_status={row.status: row.orders for row in totals},
            pieces=sum(row.pieces for row in live),
            subtotal=sum((row.subtotal for row in live), Decimal("0.00")),
            discount_total=sum((row.discount_total for row in live), Decimal("0.00")),
            total=total,
            collected=sum(paid.values(), Decimal("0.00")),
            balance=total - collected_on_live,
        )

    # -- read by the daily close (PR 10) ------------------------------------

    async def income_on(self, day: date) -> Split:
        """What tickets brought in on a date, split cash vs. transfer (D1).

        Here and not in `daily_close` because what counts as income against a
        ticket is this module's rule: the day's money is what was *collected*,
        not what was invoiced, and a ticket with an advance pays into two days.
        """
        return Split.of(await self.repository.collected_on(day))

    async def orders_delivered_on(self, day: date) -> int:
        return await self.repository.count_delivered_on(day)

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
        # Today's lock, not the ticket's: clothes taken on Monday are still handed
        # back on Wednesday after Monday is closed. What this would change is
        # *today's* count of deliveries and, if money changes hands, today's
        # income — so today is the day that has to still be open.
        await ensure_open(self.closed_days, business_date())

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
        # Voiding takes a ticket off its day's sheet, so that day has to be open.
        await ensure_open(self.closed_days, order.order_date)

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
        # `paid_at` is now, so the money lands on today's sheet whatever the
        # ticket's date: settling a Monday ticket on Wednesday is Wednesday's
        # income (D1). Today is therefore the day that must still be open.
        await ensure_open(self.closed_days, business_date())

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
