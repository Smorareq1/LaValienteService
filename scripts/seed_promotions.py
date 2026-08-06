"""Seed the promotions the business already gives (Plan 0001 §10).

Idempotent: promotions are matched by their code and an existing one is left
exactly as it is — an administrator may have corrected its value or its window,
and a re-run must not put the seeded numbers back.

    python -m scripts.seed_promotions
"""

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.modules.promotions.models import DiscountType, Promotion

#: Kept in the past so a ticket captured today finds the open-ended ones live.
VALID_FROM = date(2026, 1, 1)

#: (code, name, description, type, value, service codes, valid_from, valid_to)
SeedPromotion = tuple[
    str, str, str, DiscountType, str, tuple[str, ...] | None, date, date | None
]

PROMOTIONS: tuple[SeedPromotion, ...] = (
    (
        "domicilio_50",
        "50% en domicilio",
        "La mitad del cobro por recepción y entrega a domicilio.",
        DiscountType.PERCENTAGE,
        "50",
        ("pickup", "delivery"),
        VALID_FROM,
        None,
    ),
    (
        "edredon_q5",
        "Q5 en edredones",
        "Q5 de descuento en lavado y secado de edredones y ropa que no va por peso.",
        DiscountType.FIXED_AMOUNT,
        "5.00",
        None,
        VALID_FROM,
        None,
    ),
    (
        "edredon_jul2026",
        "Promo edredón julio 2026",
        "Lavado en tina y secado del edredón por Q35 en total.",
        DiscountType.SPECIAL_PRICE,
        "35.00",
        ("wash_tub", "dry"),
        date(2026, 7, 15),
        date(2026, 7, 30),
    ),
)


async def seed_promotions() -> None:
    async with AsyncSessionFactory() as session:
        created = 0
        for (
            code,
            name,
            description,
            discount_type,
            value,
            service_codes,
            valid_from,
            valid_to,
        ) in PROMOTIONS:
            existing = await session.scalar(select(Promotion).where(Promotion.code == code))
            if existing is not None:
                continue

            session.add(
                Promotion(
                    code=code,
                    name=name,
                    description=description,
                    discount_type=discount_type,
                    value=Decimal(value),
                    applies_to_service_codes=(
                        list(service_codes) if service_codes is not None else None
                    ),
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )
            created += 1

        await session.commit()
        print(
            f"Promotions seeded: {created} created, "
            f"{len(PROMOTIONS) - created} already there and left untouched."
        )


if __name__ == "__main__":
    asyncio.run(seed_promotions())
