"""Seed the three supplies the sheet names (Plan 0005 §8.3).

Idempotent: a product is matched by name and never rewritten, because the unit
and the description are things the business corrects from the app and a re-run
must not undo that.

Only the products. **No lots** — a lot is a purchase that happened, with a
quantity, a cost and a correlative that someone will read off a bottle. Inventing
one would put stock on the shelf that is not there, and the first sale would take
it out of a batch that never arrived. The real ones go in from the app, or from
the expense that pays for them (§6.3).

Images are not seeded either: the admin uploads them afterwards (D10).

    python -m scripts.seed_products
"""

import asyncio

from sqlalchemy import func, select

from src.core.database import AsyncSessionFactory
from src.modules.inventory.models import Product

#: (name, unit, description, sort_order)
PRODUCTS: tuple[tuple[str, str, str, int], ...] = (
    ("Detergente", "bolsa", "Detergente en polvo para lavado.", 1),
    ("Suavizante", "bote", "Suavizante de telas.", 2),
    ("Cloro", "galón", "Cloro para blanqueado y limpieza.", 3),
)


async def seed_products() -> None:
    async with AsyncSessionFactory() as session:
        created = 0
        for name, unit, description, sort_order in PRODUCTS:
            existing = await session.scalar(
                select(Product).where(func.lower(Product.name) == name.lower())
            )
            if existing is not None:
                continue
            session.add(
                Product(name=name, unit=unit, description=description, sort_order=sort_order)
            )
            created += 1

        await session.commit()
        print(f"Products seeded: {created} of {len(PRODUCTS)} created.")


if __name__ == "__main__":
    asyncio.run(seed_products())
