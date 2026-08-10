"""Seed the expense categories the sheet shows (Plan 0005 §8.3).

Idempotent: a category is matched by name and never rewritten. The list is still
open (§10 question 3 — "Secadora Q20": maintenance, tokens, rent?), so the
business renames and adds from the app and a re-run must not undo that.

Two of them are load-bearing and not just convenient: `Horas extra` is where an
overtime payment goes (§6.2 step 4) and `Compra de insumos` is where a lot
purchase is booked (§6.3). Their names live in `expenses/models.py`, and the code
looks them up by those names.

    python -m scripts.seed_expense_categories
"""

import asyncio

from sqlalchemy import func, select

from src.core.database import AsyncSessionFactory
from src.modules.expenses.models import (
    OVERTIME_CATEGORY,
    SUPPLY_PURCHASE_CATEGORY,
    ExpenseCategory,
)

#: (name, sort_order)
CATEGORIES: tuple[tuple[str, int], ...] = (
    (OVERTIME_CATEGORY, 1),
    ("Gas", 2),
    ("Transporte (moto)", 3),
    (SUPPLY_PURCHASE_CATEGORY, 4),
    ("Mantenimiento de equipo", 5),
    ("Otros", 6),
)


async def seed_expense_categories() -> None:
    async with AsyncSessionFactory() as session:
        created = 0
        for name, sort_order in CATEGORIES:
            existing = await session.scalar(
                select(ExpenseCategory).where(
                    func.lower(ExpenseCategory.name) == name.lower()
                )
            )
            if existing is not None:
                continue
            session.add(ExpenseCategory(name=name, sort_order=sort_order))
            created += 1

        await session.commit()
        print(f"Expense categories seeded: {created} of {len(CATEGORIES)} created.")


if __name__ == "__main__":
    asyncio.run(seed_expense_categories())
