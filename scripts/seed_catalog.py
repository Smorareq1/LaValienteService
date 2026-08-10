"""Seed the catalog with what the paper ticket actually charges (Plan 0001 §10).

Idempotent: services, options and garment types are matched by their natural key
and prices are only opened when none is in force, so re-running after a manual
price change does not resurrect the old price.

What it *does* reconcile is how each row **reads**: the name, the unit label and
the piece range of an option. Those are wording, not money — when the counter
asks for a different word ("Lavado", not "Lavado por tina") the change has to
reach a database that already has the row, and the alternative is renaming by
hand in production. Prices stay untouched, which is the line that matters.

The catch, and it is deliberate: this file is the source of truth for the
wording, so a rename made through the catalog screen (`PATCH
/catalog/service-types/{id}`) is reverted the next time this runs. Rename here
too, or do not run it again.

    python -m scripts.seed_catalog
"""

import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionFactory
from src.modules.catalog.models import (
    GarmentType,
    PricingMode,
    ServiceOption,
    ServicePrice,
    ServiceType,
)

#: The date the seeded prices start from. Kept in the past so an order captured
#: today always finds a price in force.
PRICES_VALID_FROM = date(2026, 1, 1)

#: (code, name, price, min_quantity, max_quantity) of one option of a tiered service.
SeedOption = tuple[str, str, str, int | None, int | None]
#: (code, name, pricing_mode, unit_label, price, options) of one service.
SeedService = tuple[str, str, PricingMode, str | None, str | None, tuple[SeedOption, ...]]

SERVICES: tuple[SeedService, ...] = (
    (
        "wash_by_weight",
        "Lavado por peso",
        PricingMode.PER_UNIT,
        "lb",
        "2.50",
        (),
    ),
    (
        "wash_tub",
        # "Lavado" a secas, como lo pide el mostrador: es el servicio de todos
        # los días y el tamaño de la tina ya lo dice la opción. La unidad sigue
        # siendo la tina, así que la línea se lee "Q30.00 por tina".
        "Lavado",
        PricingMode.TIERED,
        "tina",
        None,
        (
            # El nombre del tamaño solo: la opción se lee bajo el servicio, y
            # repetir "tina" en cada fila llena la boleta de ruido.
            ("G", "Grande", "30.00", None, None),
            ("E", "Mediano", "25.00", None, None),
            ("P", "Pequeño", "20.00", None, None),
        ),
    ),
    (
        "dry",
        "Secado",
        PricingMode.TIERED,
        "ciclo",
        None,
        (
            ("T40", "Secado 40 min", "20.00", None, None),
            ("T50", "Secado 50 min", "25.00", None, None),
            ("T60", "Secado 60 min", "30.00", None, None),
        ),
    ),
    (
        "hand_wash",
        "Lavado a mano",
        PricingMode.TIERED,
        "nivel",
        None,
        (
            ("N2", "Nivel 2 (1 a 4 piezas)", "5.00", 1, 4),
            ("N3", "Nivel 3 (5 a 9 piezas)", "10.00", 5, 9),
            ("N4", "Nivel 4 (10 a 13 piezas)", "15.00", 10, 13),
        ),
    ),
    ("extra_softener", "Rins (suavizante)", PricingMode.PER_UNIT, "aplicación", "10.00", ()),
    ("extra_spin", "Spin (centrifugado)", PricingMode.PER_UNIT, "ciclo", "5.00", ()),
    ("extra_dry_time", "Tiempo extra de secado", PricingMode.PER_UNIT, "lapso 10 min", "5.00", ()),
    ("urgent_service", "Servicio urgente", PricingMode.PER_UNIT, "pedido", "10.00", ()),
    # Variable: the courier sets the amount, so there is no catalog price.
    ("pickup", "Recepción a domicilio", PricingMode.VARIABLE, None, None, ()),
    ("delivery", "Entrega a domicilio", PricingMode.VARIABLE, None, None, ()),
)

#: The 21 garment kinds printed on the ticket, in the order they appear on it.
GARMENT_TYPES: tuple[str, ...] = (
    "Blusa",
    "Camisa",
    "Camiseta",
    "Chamarra/Poncho/Edredón",
    "Cobertor",
    "Chumpa",
    "Falda",
    "Licra",
    "Mixtos",
    "Medias",
    "Pantalón",
    "Pants/Pijama",
    "Playera/Polo",
    "Ropa de Niño",
    "Sábana",
    "Sobrefunda",
    "Short",
    "Suéter",
    "Toalla grande",
    "Toalla de manos",
    "Vestido",
)


async def seed_catalog() -> None:
    async with AsyncSessionFactory() as session:
        created_services = 0
        renamed = 0
        opened_prices = 0

        for order, (code, name, mode, unit, price, options) in enumerate(SERVICES):
            service = await session.scalar(select(ServiceType).where(ServiceType.code == code))
            if service is None:
                service = ServiceType(
                    code=code,
                    name=name,
                    pricing_mode=mode,
                    unit_label=unit,
                    sort_order=order,
                )
                session.add(service)
                await session.flush()
                created_services += 1
            else:
                renamed += _reword(service, name=name, unit_label=unit)

            option_ids: dict[str, ServiceOption] = {}
            for option_order, (
                option_code,
                option_name,
                _option_price,
                min_quantity,
                max_quantity,
            ) in enumerate(options):
                option = await session.scalar(
                    select(ServiceOption).where(
                        ServiceOption.service_type_id == service.id,
                        ServiceOption.code == option_code,
                    )
                )
                if option is None:
                    option = ServiceOption(
                        service_type_id=service.id,
                        code=option_code,
                        name=option_name,
                        min_quantity=min_quantity,
                        max_quantity=max_quantity,
                        sort_order=option_order,
                    )
                    session.add(option)
                    await session.flush()
                else:
                    renamed += _reword(
                        option,
                        name=option_name,
                        min_quantity=min_quantity,
                        max_quantity=max_quantity,
                    )
                option_ids[option_code] = option

            if price is not None:
                opened_prices += await _ensure_price(session, service.id, None, price)
            for option_code, _n, option_price, _mn, _mx in options:
                opened_prices += await _ensure_price(
                    session, service.id, option_ids[option_code].id, option_price
                )

        created_garments = 0
        for order, name in enumerate(GARMENT_TYPES):
            garment = await session.scalar(select(GarmentType).where(GarmentType.name == name))
            if garment is None:
                session.add(GarmentType(name=name, sort_order=order))
                created_garments += 1

        await session.commit()
        print(
            f"Catalog seeded: {created_services} services, {opened_prices} prices, "
            f"{created_garments} garment types, {renamed} rows reworded "
            "(prices left untouched)."
        )


def _reword(row: ServiceType | ServiceOption, **wording: object) -> int:
    """Bring an existing row's wording in line with the seed. Returns 1 if it moved.

    Only assigns what actually differs: a no-op assignment would still count as
    a change for the session hook, take a feed position and bump the version, so
    every device would re-download the whole catalog on each run of this script.
    """
    changed = False
    for field, value in wording.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return 1 if changed else 0


async def _ensure_price(
    session: AsyncSession,
    service_type_id: UUID,
    service_option_id: UUID | None,
    amount: str,
) -> int:
    """Open a price only when nothing is in force — never overwrite a real one."""
    existing = await session.scalar(
        select(ServicePrice).where(
            ServicePrice.service_type_id == service_type_id,
            ServicePrice.service_option_id.is_(service_option_id)
            if service_option_id is None
            else ServicePrice.service_option_id == service_option_id,
            ServicePrice.valid_to.is_(None),
        )
    )
    if existing is not None:
        return 0

    session.add(
        ServicePrice(
            service_type_id=service_type_id,
            service_option_id=service_option_id,
            price=Decimal(amount),
            valid_from=PRICES_VALID_FROM,
        )
    )
    return 1


if __name__ == "__main__":
    asyncio.run(seed_catalog())
