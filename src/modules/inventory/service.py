from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from src.core import media
from src.core.business_time import business_date
from src.core.exceptions import ConflictError, NotFoundError
from src.core.money import money
from src.modules.customers.service import CustomersService
from src.modules.daily_close.lock import ClosedDays, ensure_open
from src.modules.daily_close.totals import Split
from src.modules.identity.models import User
from src.modules.inventory.allocation import (
    ZERO,
    Allocation,
    LotStock,
    allocate_fifo,
    sale_total,
)
from src.modules.inventory.models import (
    MOVEMENT_SIGN,
    InventoryMovement,
    MovementType,
    Product,
    ProductLot,
    SupplySale,
    SupplySaleItem,
)
from src.modules.inventory.repository import InventoryRepository, KardexEntry
from src.modules.inventory.schemas import (
    MovementCreate,
    MovementRead,
    ProductCreate,
    ProductLotCreate,
    ProductLotRead,
    ProductLotUpdate,
    ProductRead,
    ProductUpdate,
    SupplySaleCancel,
    SupplySaleCreate,
    SupplySalePage,
    SupplySaleRead,
)
from src.modules.orders.models import PaymentMethod


def _lot_stock(lot: ProductLot) -> LotStock:
    """The flat view the FIFO arithmetic works on."""
    return LotStock(
        lot_id=lot.id,
        lot_number=lot.lot_number,
        quantity_available=lot.quantity_available,
        sale_price=lot.sale_price,
    )


class PurchaseExpenses(Protocol):
    """What `inventory` needs from `expenses` to book a purchase (§6.3).

    A protocol and not the class, so this module knows nothing about the other
    one: the foreign key runs from an expense to a lot, and an import running
    back the other way would close a circle for one method. `expenses` satisfies
    this by having the method, and `dependencies.py` is where the two meet.
    """

    async def stage_lot_purchase(
        self,
        *,
        lot_id: UUID,
        concept: str,
        amount: Decimal,
        expense_date: date,
        method: PaymentMethod,
        pending: bool,
        observations: str | None,
        actor: User,
    ) -> None: ...


def line_description(product: Product, lot_number: int) -> str:
    """What the sale line will still say a year from now.

    A snapshot, like an order charge's description: renaming the product or
    correcting its unit must not rewrite a receipt already handed over.
    """
    return f"{product.name} {product.unit} (lote {lot_number})"


class InventoryService:
    """Supplies: what is on the shelf, what it cost, and what leaves with whom.

    The arithmetic of *which* lots cover a sale is in `allocation.py`. What is
    decided here is when stock may move at all, and that nothing moves without
    leaving a line in the kardex (D4).
    """

    def __init__(
        self,
        repository: InventoryRepository,
        customers: CustomersService,
        expenses: PurchaseExpenses | None = None,
        closed_days: ClosedDays | None = None,
    ) -> None:
        self.repository = repository
        self.customers = customers
        #: Absent only where nothing books purchases — the unit tests of the
        #: allocation rules. Registering a lot *with* an expense demands it.
        self.expenses = expenses
        #: The date lock of D9, on the two writes that land on a day's sheet: a
        #: counter sale and the expense a purchase books.
        self.closed_days = closed_days

    # -- products ----------------------------------------------------------

    async def list_products(self, *, include_inactive: bool = False) -> list[ProductRead]:
        """Every product with its stock, in two queries rather than one per row."""
        products = await self.repository.list_products(include_inactive=include_inactive)
        lots = await self.repository.list_lots()
        by_product: dict[UUID, list[ProductLot]] = {}
        for lot in lots:
            by_product.setdefault(lot.product_id, []).append(lot)
        return [
            self._to_product_read(product, by_product.get(product.id, []))
            for product in products
        ]

    async def get_product(self, product_id: UUID) -> Product:
        product = await self.repository.get_product(product_id)
        if product is None:
            raise NotFoundError("Product not found.")
        return product

    async def read_product(self, product_id: UUID) -> ProductRead:
        product = await self.get_product(product_id)
        return self._to_product_read(product, await self.repository.list_lots(product_id))

    async def create_product(self, data: ProductCreate) -> ProductRead:
        await self._check_name_free(data.name, product_id=None)
        product = Product(
            name=data.name.strip(),
            unit=data.unit.strip(),
            description=data.description,
            sort_order=data.sort_order,
        )
        self.repository.add(product)
        await self.repository.commit()
        return self._to_product_read(product, [])

    async def update_product(self, product_id: UUID, data: ProductUpdate) -> ProductRead:
        product = await self.get_product(product_id)
        changes = data.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] is not None:
            await self._check_name_free(changes["name"], product_id=product_id)
            changes["name"] = changes["name"].strip()

        for field, value in changes.items():
            setattr(product, field, value)

        # Deactivating is not a tombstone, for the same reason an employee's is
        # not: lots, sales and kardex lines point at this row, and a product that
        # vanished would leave last month's sales describing nothing.
        await self.repository.commit()
        return self._to_product_read(product, await self.repository.list_lots(product_id))

    async def _check_name_free(self, name: str, *, product_id: UUID | None) -> None:
        existing = await self.repository.get_product_by_name(name)
        if existing is not None and existing.id != product_id:
            raise ConflictError(f"A product named '{existing.name}' already exists.")

    async def set_image(self, product_id: UUID, content: bytes) -> ProductRead:
        """Attach a photo to a product (D10).

        The old file is removed only after the new path is committed: a crash in
        between should leave an unused file on disk, never a row pointing at one
        that is gone.
        """
        product = await self.get_product(product_id)
        previous = product.image_path
        product.image_path = await media.store_product_image(product_id, content)
        await self.repository.commit()
        if previous and previous != product.image_path:
            await media.delete(previous)
        return self._to_product_read(product, await self.repository.list_lots(product_id))

    @staticmethod
    def _to_product_read(product: Product, lots: Sequence[ProductLot]) -> ProductRead:
        sellable = [lot for lot in lots if lot.is_sellable]
        return ProductRead(
            id=product.id,
            name=product.name,
            unit=product.unit,
            description=product.description,
            image_path=product.image_path,
            is_active=product.is_active,
            sort_order=product.sort_order,
            version=product.version,
            stock=sum((lot.quantity_available for lot in lots), ZERO),
            sellable_stock=sum((lot.quantity_available for lot in sellable), ZERO),
            # The lots arrive oldest first, so the head of the list is the one
            # FIFO would draw on next — the price the counter should quote.
            next_sale_price=sellable[0].sale_price if sellable else None,
        )

    # -- lots --------------------------------------------------------------

    async def list_lots(self, product_id: UUID) -> list[ProductLotRead]:
        await self.get_product(product_id)
        lots = await self.repository.list_lots(product_id)
        return [ProductLotRead.model_validate(lot) for lot in lots]

    async def register_lot(
        self, product_id: UUID, data: ProductLotCreate, *, actor: User
    ) -> ProductLotRead:
        """A purchase arriving: the lot, its kardex entry and — when the money is
        sent with it — the expense that paid for it, all in one transaction (§6.3).
        """
        product = await self.get_product(product_id)
        if not product.is_active:
            raise ConflictError(f"'{product.name}' is no longer in use and takes no new lots.")

        received_at = data.received_at or business_date()
        if data.expense is not None:
            # Only when money is booked with the lot: the expense lands on that
            # date's sheet (§6.3), and a closed day may not grow a new one. Stock
            # arriving on its own changes no total the close ever printed.
            await ensure_open(self.closed_days, received_at)

        lot = ProductLot(
            # Assigned here rather than left to the column default so the
            # movement below can point at it without a round trip: the two rows
            # are one event and go in together.
            id=uuid4(),
            product_id=product.id,
            lot_number=await self.repository.next_lot_number(product.id),
            quantity_received=data.quantity_received,
            quantity_available=data.quantity_received,
            unit_cost=self._resolve_unit_cost(data),
            sale_price=data.sale_price,
            received_at=received_at,
        )
        self.repository.add(lot)
        # Flushed before the movement that points at it. There is no
        # `relationship` between the two — the kardex is read by query and never
        # through a lot object — and without one SQLAlchemy has no dependency to
        # order the two inserts by, so the movement can go in first and break the
        # foreign key. The id was assigned above, so this costs one statement,
        # not a round trip to learn it.
        await self.repository.flush()
        self.repository.add(
            InventoryMovement(
                lot_id=lot.id,
                movement_type=MovementType.PURCHASE_IN,
                quantity=data.quantity_received,
                notes=data.notes,
                created_by_id=actor.id,
            )
        )

        if data.expense is not None:
            if self.expenses is None:
                raise ConflictError("This service cannot book expenses.")
            await self.expenses.stage_lot_purchase(
                lot_id=lot.id,
                concept=line_description(product, lot.lot_number),
                amount=data.expense.total,
                expense_date=received_at,
                method=data.expense.method,
                pending=data.expense.pending,
                observations=data.expense.observations,
                actor=actor,
            )

        await self.repository.commit()
        return ProductLotRead.model_validate(lot)

    @staticmethod
    def _resolve_unit_cost(data: ProductLotCreate) -> Decimal | None:
        """What one unit cost, worked out from the bill when it is not given.

        This is the line of §6.3 that answers "no hay registro de precios
        originales": from the day a purchase is booked with its expense, the
        cost per unit stops being unknown. An explicit `unit_cost` always wins —
        the invoice may cover freight the shelf should not carry.
        """
        if data.unit_cost is not None or data.expense is None:
            return data.unit_cost
        return money(data.expense.total / data.quantity_received)

    async def get_lot(self, lot_id: UUID) -> ProductLot:
        lot = await self.repository.get_lot(lot_id)
        if lot is None:
            raise NotFoundError("Product lot not found.")
        return lot

    async def update_lot(self, lot_id: UUID, data: ProductLotUpdate) -> ProductLotRead:
        """Correct what a lot cost or what it sells for.

        Not the quantities: those only move through the kardex (D4). Setting
        `sale_price` on a lot that had none is how internal stock is put on the
        counter, and clearing it takes it back off.
        """
        lot = await self.get_lot(lot_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(lot, field, value)
        await self.repository.commit()
        return ProductLotRead.model_validate(lot)

    # -- kardex ------------------------------------------------------------

    async def list_movements(
        self,
        *,
        product_id: UUID | None = None,
        lot_id: UUID | None = None,
        on_date: date | None = None,
        movement_type: MovementType | None = None,
    ) -> list[MovementRead]:
        entries = await self.repository.list_movements(
            product_id=product_id, lot_id=lot_id, on_date=on_date, movement_type=movement_type
        )
        return [self._to_movement_read(entry) for entry in entries]

    async def record_movement(self, data: MovementCreate, *, actor: User) -> MovementRead:
        """Internal use, or a correction to a count (§6.3).

        The schema already refused a `purchase_in` or a `sale_out` typed by hand;
        what is settled here is that the shelf cannot end up negative.
        """
        lot = await self.get_lot(data.lot_id)
        product = await self.get_product(lot.product_id)

        delta = data.quantity * MOVEMENT_SIGN[data.movement_type]
        remaining = lot.quantity_available + delta
        if remaining < ZERO:
            raise ConflictError(
                f"Lot {lot.lot_number} of {product.name} only has {lot.quantity_available} left."
            )

        lot.quantity_available = remaining
        movement = InventoryMovement(
            lot_id=lot.id,
            movement_type=data.movement_type,
            quantity=data.quantity,
            notes=data.notes,
            created_by_id=actor.id,
        )
        self.repository.add(movement)
        await self.repository.commit()
        return self._to_movement_read(
            KardexEntry(
                movement=movement,
                product_id=product.id,
                product_name=product.name,
                lot_number=lot.lot_number,
            )
        )

    @staticmethod
    def _to_movement_read(entry: KardexEntry) -> MovementRead:
        movement = entry.movement
        return MovementRead(
            id=movement.id,
            lot_id=movement.lot_id,
            product_id=entry.product_id,
            product_name=entry.product_name,
            lot_number=entry.lot_number,
            movement_type=movement.movement_type,
            quantity=movement.quantity,
            unit_price=movement.unit_price,
            supply_sale_item_id=movement.supply_sale_item_id,
            notes=movement.notes,
            created_by_id=movement.created_by_id,
            created_at=movement.created_at,
            stock_delta=movement.stock_delta,
        )

    # -- counter sales (D2) ------------------------------------------------

    async def get_sale(self, sale_id: UUID) -> SupplySale:
        sale = await self.repository.get_sale(sale_id)
        if sale is None:
            raise NotFoundError("Supply sale not found.")
        return sale

    async def search_sales(
        self,
        *,
        sale_date: date | None = None,
        include_cancelled: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> SupplySalePage:
        sales, total = await self.repository.search_sales(
            sale_date=sale_date,
            include_cancelled=include_cancelled,
            page=page,
            page_size=page_size,
        )
        return SupplySalePage(
            items=[SupplySaleRead.model_validate(sale) for sale in sales],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create_sale(self, data: SupplySaleCreate, *, actor: User) -> SupplySale:
        """Sell supplies over the counter — the blue rows of the sheet (D2, D5).

        The seller sends products and quantities; the lots, the prices and the
        total are all the server's answer. Everything happens in one transaction:
        a sale whose stock did not come down, or stock that came down without a
        sale, are both worse than a failed request.
        """
        if data.id is not None and await self.repository.get_sale(data.id) is not None:
            # A device re-sending an operation it never saw the answer to. The
            # sale exists; saying so is not an error the counter has to solve.
            raise ConflictError("That sale is already registered.")

        self._check_lines(data)
        sale_date = data.sale_date or business_date()
        await ensure_open(self.closed_days, sale_date)
        customer_id = await self._resolve_customer(data.customer_id)

        sale = SupplySale(
            id=data.id or uuid4(),
            sale_date=sale_date,
            customer_id=customer_id,
            nit=data.nit,
            method=data.method,
            reference=data.reference,
            total=ZERO,
            sold_by_id=actor.id,
        )

        items: list[SupplySaleItem] = []
        movements: list[InventoryMovement] = []
        allocated: list[Allocation] = []
        for line in data.lines:
            product = await self.get_product(line.product_id)
            if not product.is_active:
                raise ConflictError(f"'{product.name}' is no longer on sale.")
            lots = await self.repository.lots_for_sale(product.id)
            allocations = allocate_fifo(
                [_lot_stock(lot) for lot in lots], line.quantity, product_name=product.name
            )
            by_id = {lot.id: lot for lot in lots}
            for allocation in allocations:
                item = self._build_item(sale.id, product, allocation)
                by_id[allocation.lot_id].quantity_available -= allocation.quantity
                items.append(item)
                allocated.append(allocation)
                movements.append(
                    InventoryMovement(
                        lot_id=allocation.lot_id,
                        movement_type=MovementType.SALE_OUT,
                        quantity=allocation.quantity,
                        unit_price=allocation.unit_price,
                        supply_sale_item_id=item.id,
                        created_by_id=actor.id,
                    )
                )

        sale.items = items
        sale.total = sale_total(allocated)
        self.repository.add(sale)
        # Same reason as when a lot is registered: the movements name the lot and
        # the sale line they came from, and nothing relates those mappers, so the
        # rows they point at have to be in the database first.
        await self.repository.flush()
        for movement in movements:
            self.repository.add(movement)
        await self.repository.commit()
        return sale

    @staticmethod
    def _check_lines(data: SupplySaleCreate) -> None:
        """One line per product.

        Not a matter of taste: two lines of the same product would each be
        allocated against the stock as the database still sees it, so the second
        could be handed lots the first had already emptied. Adding the quantities
        instead would quietly change what the seller typed, which is worse at a
        counter than being told to fix it.
        """
        seen: set[UUID] = set()
        for line in data.lines:
            if line.product_id in seen:
                raise ConflictError(
                    "The same product is on the sale twice; put the whole quantity on one line."
                )
            seen.add(line.product_id)

    async def _resolve_customer(self, customer_id: UUID | None) -> UUID | None:
        """Counter sales are anonymous unless someone asks otherwise."""
        if customer_id is None:
            return None
        customer = await self.customers.get(customer_id)
        if not customer.is_active:
            raise ConflictError("That customer is archived.")
        return customer.id

    @staticmethod
    def _build_item(sale_id: UUID, product: Product, allocation: Allocation) -> SupplySaleItem:
        return SupplySaleItem(
            # Explicit, so the `sale_out` movement can name the line it came from
            # in the same flush.
            id=uuid4(),
            sale_id=sale_id,
            lot_id=allocation.lot_id,
            description=line_description(product, allocation.lot_number),
            quantity=allocation.quantity,
            unit_price=allocation.unit_price,
            amount=allocation.amount,
        )

    async def cancel_sale(
        self, sale_id: UUID, data: SupplySaleCancel, *, actor: User
    ) -> SupplySale:
        """Void a sale and put the stock back where it came from (§6.3).

        Back on the *same* lots, and as `adjustment` lines rather than by erasing
        the `sale_out` ones: the kardex is a history, and a bottle that went out
        and came back is two events. Whoever reads it later can see what happened
        instead of finding a gap.
        """
        sale = await self.get_sale(sale_id)
        if sale.is_cancelled:
            raise ConflictError("That sale is already voided.")
        # Voiding takes the money off the sheet of the day it was sold on.
        await ensure_open(self.closed_days, sale.sale_date)

        returning = await self.repository.get_lots([item.lot_id for item in sale.items])
        lots = {lot.id: lot for lot in returning}
        for item in sale.items:
            lot = lots.get(item.lot_id)
            if lot is not None:
                lot.quantity_available += item.quantity
            self.repository.add(
                InventoryMovement(
                    lot_id=item.lot_id,
                    movement_type=MovementType.ADJUSTMENT,
                    quantity=item.quantity,
                    supply_sale_item_id=item.id,
                    notes=f"Devolución por anulación de venta: {data.reason}",
                    created_by_id=actor.id,
                )
            )

        sale.cancelled_at = datetime.now(UTC)
        sale.cancelled_by_id = actor.id
        sale.cancel_reason = data.reason
        await self.repository.commit()
        return sale

    # -- read by other modules ---------------------------------------------

    async def supplies_income(self, on_date: date) -> Split:
        """What supplies brought in on a date — the sales that stand (§6.1).

        Here and not in the daily close because the rule of what counts as a live
        sale belongs to this module. Split by method because the arqueo needs it:
        a detergent paid by transfer never reached the drawer.
        """
        sales, _ = await self.repository.search_sales(
            sale_date=on_date, include_cancelled=False, page=1, page_size=10_000
        )
        return Split.of((sale.total, sale.method) for sale in sales)
