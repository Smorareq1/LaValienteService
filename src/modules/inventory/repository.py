from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.business_time import business_day_bounds
from src.modules.inventory.models import (
    InventoryMovement,
    MovementType,
    Product,
    ProductLot,
    SupplySale,
    SupplySaleItem,
)


@dataclass(frozen=True)
class KardexEntry:
    """A movement with the two things it is always read next to.

    The kardex is a list of "what happened to *what*", so the lot number and the
    product's name come back with the row instead of being looked up per line.
    """

    movement: InventoryMovement
    product_id: UUID
    product_name: str
    lot_number: int


class InventoryRepository:
    """Persistence for products, lots, sales and the kardex. No rules, no money."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- products ----------------------------------------------------------

    async def list_products(self, *, include_inactive: bool = False) -> list[Product]:
        statement = select(Product).where(Product.deleted_at.is_(None))
        if not include_inactive:
            statement = statement.where(Product.is_active.is_(True))
        statement = statement.order_by(Product.sort_order, Product.name)
        return list((await self.session.scalars(statement)).all())

    async def get_product(self, product_id: UUID) -> Product | None:
        statement = select(Product).where(
            Product.id == product_id, Product.deleted_at.is_(None)
        )
        product: Product | None = await self.session.scalar(statement)
        return product

    async def get_product_by_name(self, name: str) -> Product | None:
        """Case-insensitive: "Detergente" and "detergente" are one product."""
        statement = select(Product).where(
            func.lower(Product.name) == name.strip().lower(), Product.deleted_at.is_(None)
        )
        product: Product | None = await self.session.scalar(statement)
        return product

    # -- lots --------------------------------------------------------------

    @staticmethod
    def _lots_in_order() -> Select[tuple[ProductLot]]:
        """Every live lot, oldest first — the order FIFO and the stock read need.

        `lot_number` breaks the tie inside a day, because two lots received on
        the same date still arrived in an order and the correlative is it.
        """
        return (
            select(ProductLot)
            .where(ProductLot.deleted_at.is_(None))
            .order_by(ProductLot.product_id, ProductLot.received_at, ProductLot.lot_number)
        )

    async def list_lots(self, product_id: UUID | None = None) -> list[ProductLot]:
        statement = self._lots_in_order()
        if product_id is not None:
            statement = statement.where(ProductLot.product_id == product_id)
        return list((await self.session.scalars(statement)).all())

    async def lots_for_sale(self, product_id: UUID) -> list[ProductLot]:
        """The lots a sale may draw on, locked until the transaction ends.

        `FOR UPDATE` and not a plain read: two counters selling the last bottle
        at the same second would both see it available, and the loser would hit
        the `quantity_available >= 0` check as a 500 instead of being told there
        is one left. The lock is held for the length of one small transaction.
        """
        statement = (
            self._lots_in_order()
            .where(
                ProductLot.product_id == product_id,
                ProductLot.quantity_available > 0,
                ProductLot.sale_price.is_not(None),
            )
            .with_for_update()
        )
        return list((await self.session.scalars(statement)).all())

    async def get_lot(self, lot_id: UUID) -> ProductLot | None:
        statement = select(ProductLot).where(
            ProductLot.id == lot_id, ProductLot.deleted_at.is_(None)
        )
        lot: ProductLot | None = await self.session.scalar(statement)
        return lot

    async def get_lots(self, lot_ids: list[UUID]) -> list[ProductLot]:
        """Several lots at once — what voiding a sale needs to give stock back."""
        if not lot_ids:
            return []
        statement = (
            select(ProductLot)
            .where(ProductLot.id.in_(lot_ids), ProductLot.deleted_at.is_(None))
            .with_for_update()
        )
        return list((await self.session.scalars(statement)).all())

    async def next_lot_number(self, product_id: UUID) -> int:
        """The next correlative of this product (D3).

        Archived lots still count, for the same reason a voided ticket keeps its
        daily number: "Suavizante #14" is written on paper somewhere.
        """
        highest = await self.session.scalar(
            select(func.max(ProductLot.lot_number)).where(ProductLot.product_id == product_id)
        )
        return (highest or 0) + 1

    # -- sales -------------------------------------------------------------

    @staticmethod
    def _with_items() -> Select[tuple[SupplySale]]:
        return select(SupplySale).options(
            selectinload(SupplySale.items.and_(SupplySaleItem.deleted_at.is_(None)))
        )

    async def get_sale(self, sale_id: UUID) -> SupplySale | None:
        statement = self._with_items().where(
            SupplySale.id == sale_id, SupplySale.deleted_at.is_(None)
        )
        sale: SupplySale | None = await self.session.scalar(statement)
        return sale

    async def search_sales(
        self,
        *,
        sale_date: date | None = None,
        include_cancelled: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[SupplySale], int]:
        statement = select(SupplySale).where(SupplySale.deleted_at.is_(None))
        if sale_date is not None:
            statement = statement.where(SupplySale.sale_date == sale_date)
        if not include_cancelled:
            statement = statement.where(SupplySale.cancelled_at.is_(None))

        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))
        page_statement = (
            statement.order_by(SupplySale.sale_date.desc(), SupplySale.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .options(selectinload(SupplySale.items.and_(SupplySaleItem.deleted_at.is_(None))))
        )
        items = list((await self.session.scalars(page_statement)).all())
        return items, total or 0

    # -- kardex ------------------------------------------------------------

    async def list_movements(
        self,
        *,
        product_id: UUID | None = None,
        lot_id: UUID | None = None,
        on_date: date | None = None,
        movement_type: MovementType | None = None,
        limit: int = 200,
    ) -> list[KardexEntry]:
        """The kardex, newest first, with each line's product already attached.

        `on_date` is a *business* date: a movement is stamped in UTC and an
        adjustment made at seven in the evening in Cobán is already tomorrow
        there, which would put it in the wrong day's kardex.
        """
        statement = (
            select(InventoryMovement, ProductLot.product_id, Product.name, ProductLot.lot_number)
            .join(ProductLot, ProductLot.id == InventoryMovement.lot_id)
            .join(Product, Product.id == ProductLot.product_id)
            .where(InventoryMovement.deleted_at.is_(None))
        )
        if product_id is not None:
            statement = statement.where(ProductLot.product_id == product_id)
        if lot_id is not None:
            statement = statement.where(InventoryMovement.lot_id == lot_id)
        if movement_type is not None:
            statement = statement.where(InventoryMovement.movement_type == movement_type)
        if on_date is not None:
            start, end = business_day_bounds(on_date)
            statement = statement.where(
                InventoryMovement.created_at >= start, InventoryMovement.created_at < end
            )

        statement = statement.order_by(InventoryMovement.created_at.desc()).limit(limit)
        rows = await self.session.execute(statement)
        return [
            KardexEntry(
                movement=movement,
                product_id=lot_product_id,
                product_name=product_name,
                lot_number=lot_number,
            )
            for movement, lot_product_id, product_name, lot_number in rows
        ]

    # -- writes ------------------------------------------------------------

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
