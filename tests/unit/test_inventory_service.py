"""The rules that sit above the FIFO arithmetic (Plan 0005 §6.3, D3 to D5).

What is exercised here is what the service adds: stock that only ever moves
through the kardex, lot correlatives per product, a voided sale putting bottles
back on the lots they left, and the counter being told what is actually on the
shelf instead of hitting a check constraint.

No database. The fake repository below stands in for one, and it stamps the
columns PostgreSQL would fill on flush.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.customers.models import Customer
from src.modules.customers.service import CustomersService
from src.modules.daily_close.lock import ClosedDays
from src.modules.identity.models import User
from src.modules.inventory.allocation import InsufficientStock
from src.modules.inventory.models import (
    InventoryMovement,
    MovementType,
    Product,
    ProductLot,
    SupplySale,
)
from src.modules.inventory.repository import InventoryRepository, KardexEntry
from src.modules.inventory.schemas import (
    LotPurchaseExpense,
    MovementCreate,
    ProductCreate,
    ProductLotCreate,
    ProductUpdate,
    SupplySaleCancel,
    SupplySaleCreate,
    SupplySaleLineCreate,
)
from src.modules.inventory.service import InventoryService, PurchaseExpenses
from src.modules.orders.models import PaymentMethod

SALE_DATE = date(2026, 7, 18)
ACTOR_ID = uuid4()


class FakeUser:
    """Only `.id` is ever read by this module; a real `User` needs a database."""

    id = ACTOR_ID


#: Cast once instead of silencing the type checker at forty call sites.
ACTOR = cast(User, FakeUser())


class FakeCustomersService:
    """Only what inventory asks of it: does this customer exist, and is it live."""

    def __init__(self, *, active: bool = True) -> None:
        self.active = active

    async def get(self, customer_id: UUID) -> Customer:
        customer = Customer(full_name="Marta González")
        customer.id = customer_id
        customer.is_active = self.active
        return customer


class FakeInventoryRepository:
    """In-memory stand-in for `InventoryRepository`.

    `add` stamps `id`, `version` and `created_at` because those are column
    defaults: PostgreSQL fills them at flush, and without them the read schemas
    would fail validation on rows that are perfectly fine in production.
    """

    def __init__(self) -> None:
        self.products: list[Product] = []
        self.lots: list[ProductLot] = []
        self.sales: list[SupplySale] = []
        self.movements: list[InventoryMovement] = []
        self.commits = 0

    # -- products

    async def list_products(self, *, include_inactive: bool = False) -> list[Product]:
        return [
            product
            for product in self.products
            if include_inactive or product.is_active
        ]

    async def get_product(self, product_id: UUID) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    async def get_product_by_name(self, name: str) -> Product | None:
        wanted = name.strip().lower()
        return next((p for p in self.products if p.name.strip().lower() == wanted), None)

    # -- lots

    def _ordered(self, lots: list[ProductLot]) -> list[ProductLot]:
        return sorted(lots, key=lambda lot: (lot.received_at, lot.lot_number))

    async def list_lots(self, product_id: UUID | None = None) -> list[ProductLot]:
        lots = [lot for lot in self.lots if product_id is None or lot.product_id == product_id]
        return self._ordered(lots)

    async def lots_for_sale(self, product_id: UUID) -> list[ProductLot]:
        return self._ordered(
            [
                lot
                for lot in self.lots
                if lot.product_id == product_id
                and lot.sale_price is not None
                and lot.quantity_available > 0
            ]
        )

    async def get_lot(self, lot_id: UUID) -> ProductLot | None:
        return next((lot for lot in self.lots if lot.id == lot_id), None)

    async def get_lots(self, lot_ids: list[UUID]) -> list[ProductLot]:
        return [lot for lot in self.lots if lot.id in set(lot_ids)]

    async def next_lot_number(self, product_id: UUID) -> int:
        numbers = [lot.lot_number for lot in self.lots if lot.product_id == product_id]
        return max(numbers, default=0) + 1

    # -- sales

    async def get_sale(self, sale_id: UUID) -> SupplySale | None:
        return next((sale for sale in self.sales if sale.id == sale_id), None)

    async def search_sales(
        self,
        *,
        sale_date: date | None = None,
        include_cancelled: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[SupplySale], int]:
        found = [
            sale
            for sale in self.sales
            if (sale_date is None or sale.sale_date == sale_date)
            and (include_cancelled or not sale.is_cancelled)
        ]
        start = (page - 1) * page_size
        return found[start : start + page_size], len(found)

    # -- kardex

    async def list_movements(self, **_: object) -> list[KardexEntry]:
        entries: list[KardexEntry] = []
        for movement in self.movements:
            lot = await self.get_lot(movement.lot_id)
            assert lot is not None
            product = await self.get_product(lot.product_id)
            assert product is not None
            entries.append(
                KardexEntry(
                    movement=movement,
                    product_id=product.id,
                    product_name=product.name,
                    lot_number=lot.lot_number,
                )
            )
        return entries

    # -- writes

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = uuid4()
        if getattr(instance, "version", None) is None:
            instance.version = 1
        if hasattr(instance, "created_at") and getattr(instance, "created_at", None) is None:
            instance.created_at = datetime.now(UTC)

        if isinstance(instance, Product):
            self.products.append(instance)
        elif isinstance(instance, ProductLot):
            self.lots.append(instance)
        elif isinstance(instance, SupplySale):
            for item in instance.items:
                if item.id is None:
                    item.id = uuid4()
                item.version = 1
            self.sales.append(instance)
        elif isinstance(instance, InventoryMovement):
            self.movements.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakePurchaseExpenses:
    """Stands in for `ExpensesService` at the one method inventory asks of it.

    That the protocol is small enough to fake in six lines is the point of
    declaring it in `inventory.service` instead of importing the real class.
    """

    def __init__(self) -> None:
        self.staged: list[dict[str, Any]] = []

    async def stage_lot_purchase(self, **kwargs: Any) -> None:
        self.staged.append(kwargs)


class FakeClosedDays:
    """The `ClosedDays` of `daily_close/lock.py`."""

    def __init__(self, *days: date) -> None:
        self.days = set(days)

    async def is_closed(self, day: date) -> bool:
        return day in self.days


def make_service(
    *,
    customer_active: bool = True,
    expenses: FakePurchaseExpenses | None = None,
    closed: FakeClosedDays | None = None,
) -> tuple[InventoryService, FakeInventoryRepository]:
    repository = FakeInventoryRepository()
    service = InventoryService(
        cast(InventoryRepository, repository),
        cast(CustomersService, FakeCustomersService(active=customer_active)),
        cast(PurchaseExpenses, expenses) if expenses is not None else None,
        closed,
    )
    return service, repository


def make_product(
    repository: FakeInventoryRepository, name: str = "Detergente", *, unit: str = "bolsa"
) -> Product:
    product = Product(name=name, unit=unit, sort_order=1)
    product.id = uuid4()
    product.version = 1
    product.is_active = True
    product.image_path = None
    product.description = None
    repository.products.append(product)
    return product


def make_lot(
    repository: FakeInventoryRepository,
    product: Product,
    *,
    number: int,
    available: str,
    price: str | None = "10.00",
    cost: str | None = None,
    received: date = date(2026, 7, 1),
) -> ProductLot:
    lot = ProductLot(
        product_id=product.id,
        lot_number=number,
        quantity_received=Decimal(available),
        quantity_available=Decimal(available),
        unit_cost=None if cost is None else Decimal(cost),
        sale_price=None if price is None else Decimal(price),
        received_at=received,
    )
    lot.id = uuid4()
    lot.version = 1
    repository.lots.append(lot)
    return lot


def sale_of(product: Product, quantity: str, **kwargs: Any) -> SupplySaleCreate:
    return SupplySaleCreate(
        sale_date=SALE_DATE,
        lines=[SupplySaleLineCreate(product_id=product.id, quantity=Decimal(quantity))],
        **kwargs,
    )


class TestSelling:
    async def test_a_sale_takes_stock_out_and_writes_the_kardex(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=7, available="10.00", price="10.00")

        sale = await service.create_sale(sale_of(product, "5.00"), actor=ACTOR)

        assert sale.total == Decimal("50.00")
        assert lot.quantity_available == Decimal("5.00")
        assert len(repository.movements) == 1
        movement = repository.movements[0]
        assert movement.movement_type is MovementType.SALE_OUT
        assert movement.stock_delta == Decimal("-5.00")
        assert movement.supply_sale_item_id == sale.items[0].id

    def test_the_line_says_what_it_was_when_it_was_sold(self) -> None:
        """The description is a snapshot, like an order charge's (D5).

        Renaming the product tomorrow must not rewrite a receipt already handed
        over, so the lot number goes into the text rather than being looked up.
        """
        from src.modules.inventory.service import line_description

        product = Product(name="Suavizante", unit="bote")
        assert line_description(product, 14) == "Suavizante bote (lote 14)"

    async def test_a_sale_spanning_two_lots_is_two_lines(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(
            repository, product, number=7, available="2.00", price="10.00",
            received=date(2026, 7, 1),
        )
        make_lot(
            repository, product, number=8, available="10.00", price="12.00",
            received=date(2026, 7, 10),
        )

        sale = await service.create_sale(sale_of(product, "3.00"), actor=ACTOR)

        assert [item.unit_price for item in sale.items] == [Decimal("10.00"), Decimal("12.00")]
        assert sale.total == Decimal("32.00")
        assert len(repository.movements) == 2

    async def test_a_lot_kept_for_internal_use_is_never_sold(self) -> None:
        service, repository = make_service()
        product = make_product(repository, "Suavizante", unit="bote")
        internal = make_lot(repository, product, number=1, available="50.00", price=None)
        make_lot(
            repository, product, number=2, available="5.00", price="12.00",
            received=date(2026, 7, 10),
        )

        sale = await service.create_sale(sale_of(product, "4.00"), actor=ACTOR)

        assert internal.quantity_available == Decimal("50.00")
        assert sale.items[0].unit_price == Decimal("12.00")

    async def test_not_enough_stock_writes_nothing(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=7, available="2.00")

        with pytest.raises(InsufficientStock):
            await service.create_sale(sale_of(product, "5.00"), actor=ACTOR)

        assert repository.sales == []
        assert repository.movements == []
        assert lot.quantity_available == Decimal("2.00")

    async def test_the_same_product_twice_is_refused(self) -> None:
        """Not fussiness: each line is allocated against the stock as the database
        still sees it, so the second could be handed lots the first emptied."""
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")

        data = SupplySaleCreate(
            sale_date=SALE_DATE,
            lines=[
                SupplySaleLineCreate(product_id=product.id, quantity=Decimal("1.00")),
                SupplySaleLineCreate(product_id=product.id, quantity=Decimal("2.00")),
            ],
        )
        with pytest.raises(ConflictError, match="twice"):
            await service.create_sale(data, actor=ACTOR)

    async def test_a_product_out_of_use_is_not_on_sale(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        product.is_active = False

        with pytest.raises(ConflictError):
            await service.create_sale(sale_of(product, "1.00"), actor=ACTOR)

    async def test_an_unknown_product_is_not_found(self) -> None:
        service, _repository = make_service()
        ghost = Product(name="Fantasma", unit="bote")
        ghost.id = uuid4()

        with pytest.raises(NotFoundError):
            await service.create_sale(sale_of(ghost, "1.00"), actor=ACTOR)

    async def test_resending_a_sale_the_device_already_sent_is_a_conflict(self) -> None:
        """Offline capture generates the id, so the second copy is recognisable."""
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        sale_id = uuid4()

        await service.create_sale(sale_of(product, "1.00", id=sale_id), actor=ACTOR)
        with pytest.raises(ConflictError, match="already registered"):
            await service.create_sale(sale_of(product, "1.00", id=sale_id), actor=ACTOR)

        assert len(repository.sales) == 1

    async def test_an_archived_customer_cannot_be_put_on_the_sale(self) -> None:
        service, repository = make_service(customer_active=False)
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")

        with pytest.raises(ConflictError, match="archived"):
            await service.create_sale(
                sale_of(product, "1.00", customer_id=uuid4()), actor=ACTOR
            )

    async def test_a_counter_sale_needs_no_customer(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")

        sale = await service.create_sale(sale_of(product, "1.00"), actor=ACTOR)

        assert sale.customer_id is None
        assert sale.sold_by_id == ACTOR_ID


class TestVoiding:
    async def test_voiding_puts_the_stock_back_on_the_same_lots(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=7, available="10.00")
        sale = await service.create_sale(sale_of(product, "4.00"), actor=ACTOR)

        voided = await service.cancel_sale(
            sale.id, SupplySaleCancel(reason="Se equivocaron de producto"), actor=ACTOR
        )

        assert lot.quantity_available == Decimal("10.00")
        assert voided.is_cancelled
        assert voided.cancel_reason == "Se equivocaron de producto"

    async def test_the_return_is_a_new_line_and_not_an_erased_one(self) -> None:
        """The kardex is a history: a bottle that went out and came back is two
        things that happened, and a reader must be able to see both."""
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        sale = await service.create_sale(sale_of(product, "4.00"), actor=ACTOR)

        await service.cancel_sale(sale.id, SupplySaleCancel(reason="Anulada"), actor=ACTOR)

        kinds = [movement.movement_type for movement in repository.movements]
        assert kinds == [MovementType.SALE_OUT, MovementType.ADJUSTMENT]
        assert repository.movements[1].stock_delta == Decimal("4.00")
        assert "Anulada" in (repository.movements[1].notes or "")

    async def test_voiding_twice_is_refused(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        sale = await service.create_sale(sale_of(product, "1.00"), actor=ACTOR)
        await service.cancel_sale(sale.id, SupplySaleCancel(reason="Anulada"), actor=ACTOR)

        with pytest.raises(ConflictError, match="already voided"):
            await service.cancel_sale(
                sale.id, SupplySaleCancel(reason="Otra vez"), actor=ACTOR
            )

    async def test_a_voided_sale_stops_counting_as_income(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        await service.create_sale(sale_of(product, "5.00"), actor=ACTOR)
        second = await service.create_sale(sale_of(product, "2.00"), actor=ACTOR)

        assert (await service.supplies_income(SALE_DATE)).total == Decimal("70.00")
        await service.cancel_sale(second.id, SupplySaleCancel(reason="Anulada"), actor=ACTOR)
        assert (await service.supplies_income(SALE_DATE)).total == Decimal("50.00")

    async def test_income_keeps_the_two_ways_the_money_came_in_apart(self) -> None:
        """The arqueo of §6.1: the pink row of the sheet is a transfer, and the
        drawer can only be counted against the rest."""
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        await service.create_sale(sale_of(product, "5.00"), actor=ACTOR)
        await service.create_sale(
            sale_of(product, "2.00", method=PaymentMethod.TRANSFER), actor=ACTOR
        )

        income = await service.supplies_income(SALE_DATE)
        assert income.cash == Decimal("50.00")
        assert income.transfer == Decimal("20.00")
        assert income.total == Decimal("70.00")


class TestLots:
    async def test_registering_a_lot_numbers_it_and_writes_its_arrival(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=1, available="5.00")

        lot = await service.register_lot(
            product.id,
            ProductLotCreate(
                quantity_received=Decimal("12.00"),
                unit_cost=Decimal("7.50"),
                sale_price=Decimal("10.00"),
                received_at=SALE_DATE,
                notes="Factura 3341",
            ),
            actor=ACTOR,
        )

        assert lot.lot_number == 2
        assert lot.quantity_available == Decimal("12.00")
        assert repository.movements[0].movement_type is MovementType.PURCHASE_IN
        assert repository.movements[0].notes == "Factura 3341"

    async def test_the_correlative_is_per_product(self) -> None:
        """"Suavizante #14" is a number on a bottle, not a row id (D3)."""
        service, repository = make_service()
        detergent = make_product(repository, "Detergente")
        softener = make_product(repository, "Suavizante", unit="bote")
        make_lot(repository, detergent, number=1, available="5.00")
        make_lot(repository, detergent, number=2, available="5.00")

        lot = await service.register_lot(
            softener.id,
            ProductLotCreate(quantity_received=Decimal("1.00")),
            actor=ACTOR,
        )

        assert lot.lot_number == 1

    async def test_a_product_out_of_use_takes_no_new_lots(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        product.is_active = False

        with pytest.raises(ConflictError):
            await service.register_lot(
                product.id, ProductLotCreate(quantity_received=Decimal("1.00")), actor=ACTOR
            )

    async def test_a_lot_with_no_price_is_stock_but_not_sellable_stock(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=1, available="50.00", price=None)
        make_lot(
            repository, product, number=2, available="5.00", price="12.00",
            received=date(2026, 7, 10),
        )

        read = await service.read_product(product.id)

        assert read.stock == Decimal("55.00")
        assert read.sellable_stock == Decimal("5.00")
        assert read.next_sale_price == Decimal("12.00")

    async def test_the_quoted_price_is_the_oldest_sellable_lot(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(
            repository, product, number=1, available="2.00", price="10.00",
            received=date(2026, 7, 1),
        )
        make_lot(
            repository, product, number=2, available="9.00", price="12.00",
            received=date(2026, 7, 10),
        )

        read = await service.read_product(product.id)

        assert read.next_sale_price == Decimal("10.00")

    async def test_a_product_with_nothing_to_sell_quotes_nothing(self) -> None:
        service, repository = make_service()
        product = make_product(repository)

        read = await service.read_product(product.id)

        assert read.stock == Decimal("0.00")
        assert read.next_sale_price is None


class TestBuyingALot:
    """The purchase side of §6.3: the shelf and the money go in together."""

    async def test_the_expense_is_booked_with_the_lot(self) -> None:
        expenses = FakePurchaseExpenses()
        service, repository = make_service(expenses=expenses)
        product = make_product(repository)

        lot = await service.register_lot(
            product.id,
            ProductLotCreate(
                quantity_received=Decimal("12.00"),
                sale_price=Decimal("15.00"),
                received_at=SALE_DATE,
                expense=LotPurchaseExpense(total=Decimal("120.00")),
            ),
            actor=ACTOR,
        )

        assert len(expenses.staged) == 1
        staged = expenses.staged[0]
        assert staged["lot_id"] == lot.id
        assert staged["amount"] == Decimal("120.00")
        assert staged["expense_date"] == SALE_DATE
        assert staged["concept"] == "Detergente bolsa (lote 1)"

    async def test_the_bill_is_what_finally_gives_a_lot_its_cost(self) -> None:
        """"No hay registro de precios originales" stops being true here: twelve
        bags for Q120 is Q10 a bag, and nobody had to type it twice."""
        expenses = FakePurchaseExpenses()
        service, repository = make_service(expenses=expenses)
        product = make_product(repository)

        lot = await service.register_lot(
            product.id,
            ProductLotCreate(
                quantity_received=Decimal("12.00"),
                expense=LotPurchaseExpense(total=Decimal("120.00")),
            ),
            actor=ACTOR,
        )

        assert lot.unit_cost == Decimal("10.00")

    async def test_a_cost_that_was_typed_in_wins_over_the_bill(self) -> None:
        """The invoice may carry freight the shelf should not."""
        expenses = FakePurchaseExpenses()
        service, repository = make_service(expenses=expenses)
        product = make_product(repository)

        lot = await service.register_lot(
            product.id,
            ProductLotCreate(
                quantity_received=Decimal("12.00"),
                unit_cost=Decimal("9.00"),
                expense=LotPurchaseExpense(total=Decimal("120.00")),
            ),
            actor=ACTOR,
        )

        assert lot.unit_cost == Decimal("9.00")

    async def test_a_lot_registered_without_money_books_nothing(self) -> None:
        expenses = FakePurchaseExpenses()
        service, repository = make_service(expenses=expenses)
        product = make_product(repository)

        lot = await service.register_lot(
            product.id, ProductLotCreate(quantity_received=Decimal("5.00")), actor=ACTOR
        )

        assert expenses.staged == []
        assert lot.unit_cost is None


class TestMovements:
    async def test_internal_use_takes_stock_out(self) -> None:
        service, repository = make_service()
        product = make_product(repository, "Suavizante", unit="bote")
        lot = make_lot(repository, product, number=1, available="10.00", price=None)

        read = await service.record_movement(
            MovementCreate(
                lot_id=lot.id,
                movement_type=MovementType.INTERNAL_USE,
                quantity=Decimal("2.00"),
                notes="Suavizante usado en los pedidos del día",
            ),
            actor=ACTOR,
        )

        assert lot.quantity_available == Decimal("8.00")
        assert read.stock_delta == Decimal("-2.00")
        assert read.product_name == "Suavizante"
        assert read.lot_number == 1

    async def test_taking_out_more_than_is_there_is_refused(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=3, available="2.00")

        with pytest.raises(ConflictError, match="only has"):
            await service.record_movement(
                MovementCreate(
                    lot_id=lot.id,
                    movement_type=MovementType.INTERNAL_USE,
                    quantity=Decimal("3.00"),
                ),
                actor=ACTOR,
            )

        assert lot.quantity_available == Decimal("2.00")
        assert repository.movements == []

    async def test_a_count_that_came_up_short_is_a_negative_adjustment(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=1, available="10.00")

        await service.record_movement(
            MovementCreate(
                lot_id=lot.id,
                movement_type=MovementType.ADJUSTMENT,
                quantity=Decimal("-1.00"),
                notes="Se quebró un bote",
            ),
            actor=ACTOR,
        )

        assert lot.quantity_available == Decimal("9.00")

    async def test_a_count_that_found_more_is_a_positive_adjustment(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        lot = make_lot(repository, product, number=1, available="10.00")

        await service.record_movement(
            MovementCreate(
                lot_id=lot.id,
                movement_type=MovementType.ADJUSTMENT,
                quantity=Decimal("2.00"),
                notes="Aparecieron dos",
            ),
            actor=ACTOR,
        )

        assert lot.quantity_available == Decimal("12.00")

    def test_a_purchase_cannot_be_typed_in_by_hand(self) -> None:
        """It comes from registering a lot, which is what gives it a correlative
        and a cost. Typed in here it would be stock with no document behind it."""
        with pytest.raises(ValueError, match="written by the system"):
            MovementCreate(
                lot_id=uuid4(),
                movement_type=MovementType.PURCHASE_IN,
                quantity=Decimal("1.00"),
            )

    def test_a_sale_cannot_be_typed_in_by_hand(self) -> None:
        with pytest.raises(ValueError, match="written by the system"):
            MovementCreate(
                lot_id=uuid4(), movement_type=MovementType.SALE_OUT, quantity=Decimal("1.00")
            )

    def test_internal_use_carries_no_minus_sign(self) -> None:
        with pytest.raises(ValueError, match="how much"):
            MovementCreate(
                lot_id=uuid4(),
                movement_type=MovementType.INTERNAL_USE,
                quantity=Decimal("-1.00"),
            )

    def test_a_movement_of_zero_changes_nothing(self) -> None:
        with pytest.raises(ValueError, match="changes nothing"):
            MovementCreate(
                lot_id=uuid4(),
                movement_type=MovementType.ADJUSTMENT,
                quantity=Decimal("0.00"),
            )


class TestProducts:
    async def test_two_products_cannot_share_a_name(self) -> None:
        service, repository = make_service()
        make_product(repository, "Detergente")

        with pytest.raises(ConflictError, match="already exists"):
            await service.create_product(ProductCreate(name="detergente", unit="bolsa"))

    async def test_taking_a_product_out_of_use_is_not_a_tombstone(self) -> None:
        """Lots, sales and kardex lines point at this row: dropping it would leave
        last month's sales describing nothing."""
        service, repository = make_service()
        product = make_product(repository)

        read = await service.update_product(product.id, ProductUpdate(is_active=False))

        assert read.is_active is False
        assert product.deleted_at is None


class TestTheDateLock:
    """D9 on the two writes of this module that land on a day's sheet: the
    counter sale, and the expense a purchase books."""

    async def test_nothing_is_sold_onto_a_day_already_closed(self) -> None:
        service, repository = make_service(closed=FakeClosedDays(SALE_DATE))
        product = make_product(repository)
        lot = make_lot(repository, product, number=7, available="10.00")

        with pytest.raises(ConflictError, match="already closed"):
            await service.create_sale(sale_of(product, "2.00"), actor=ACTOR)

        assert lot.quantity_available == Decimal("10.00")
        assert repository.sales == []

    async def test_a_sale_is_not_voided_off_a_closed_day(self) -> None:
        service, repository = make_service()
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")
        sale = await service.create_sale(sale_of(product, "2.00"), actor=ACTOR)

        service.closed_days = cast(ClosedDays, FakeClosedDays(SALE_DATE))

        with pytest.raises(ConflictError, match="already closed"):
            await service.cancel_sale(sale.id, SupplySaleCancel(reason="Error"), actor=ACTOR)

    async def test_a_purchase_does_not_book_money_onto_a_closed_day(self) -> None:
        expenses = FakePurchaseExpenses()
        service, repository = make_service(
            expenses=expenses, closed=FakeClosedDays(SALE_DATE)
        )
        product = make_product(repository)

        with pytest.raises(ConflictError, match="already closed"):
            await service.register_lot(
                product.id,
                ProductLotCreate(
                    quantity_received=Decimal("12.00"),
                    received_at=SALE_DATE,
                    expense=LotPurchaseExpense(total=Decimal("120.00")),
                ),
                actor=ACTOR,
            )

        assert expenses.staged == []

    async def test_stock_arriving_on_its_own_is_not_the_close_business(self) -> None:
        """A lot with no expense attached changes no total the acta ever printed,
        so a closed day does not stand in the way of putting it on the shelf."""
        service, repository = make_service(closed=FakeClosedDays(SALE_DATE))
        product = make_product(repository)

        lot = await service.register_lot(
            product.id,
            ProductLotCreate(quantity_received=Decimal("12.00"), received_at=SALE_DATE),
            actor=ACTOR,
        )

        assert lot.quantity_available == Decimal("12.00")

    async def test_an_open_day_sells_as_usual(self) -> None:
        service, repository = make_service(closed=FakeClosedDays(date(2026, 7, 17)))
        product = make_product(repository)
        make_lot(repository, product, number=7, available="10.00")

        sale = await service.create_sale(sale_of(product, "2.00"), actor=ACTOR)

        assert sale.total == Decimal("20.00")
