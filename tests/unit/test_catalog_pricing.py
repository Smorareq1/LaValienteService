"""Price resolution — the rule the whole ticket total rests on (Plan 0001 D1/D2)."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.exceptions import ConflictError, NotFoundError
from src.modules.catalog.models import PricingMode, ServiceOption, ServicePrice, ServiceType
from src.modules.catalog.schemas import ServicePriceCreate
from src.modules.catalog.service import CatalogService, resolve_price

SERVICE_ID = uuid4()
OPTION_ID = uuid4()


def price(
    amount: str,
    valid_from: date,
    valid_to: date | None = None,
    *,
    option_id=None,
) -> ServicePrice:
    return ServicePrice(
        service_type_id=SERVICE_ID,
        service_option_id=option_id,
        price=Decimal(amount),
        valid_from=valid_from,
        valid_to=valid_to,
    )


class TestResolvePrice:
    def test_picks_the_window_covering_the_date(self) -> None:
        prices = [
            price("2.50", date(2026, 1, 1), date(2026, 6, 30)),
            price("3.00", date(2026, 7, 1)),
        ]

        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 3, 15)) == Decimal("2.50")
        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 7, 22)) == Decimal("3.00")

    def test_boundaries_are_inclusive(self) -> None:
        prices = [price("2.50", date(2026, 1, 1), date(2026, 6, 30))]

        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 1, 1)) == Decimal("2.50")
        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 6, 30)) == Decimal("2.50")

    def test_returns_none_before_the_catalog_had_a_price(self) -> None:
        """An order dated before any price window must not silently cost zero."""
        prices = [price("2.50", date(2026, 1, 1))]

        assert resolve_price(prices, (SERVICE_ID, None), date(2025, 12, 31)) is None

    def test_option_prices_do_not_leak_into_the_service_price(self) -> None:
        prices = [price("30.00", date(2026, 1, 1), option_id=OPTION_ID)]

        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 3, 1)) is None
        assert resolve_price(prices, (SERVICE_ID, OPTION_ID), date(2026, 3, 1)) == Decimal("30.00")

    def test_on_overlapping_windows_the_newest_start_wins(self) -> None:
        """Should not happen, but a bad import must not price at random."""
        prices = [
            price("2.50", date(2026, 1, 1)),
            price("3.00", date(2026, 5, 1)),
        ]

        assert resolve_price(prices, (SERVICE_ID, None), date(2026, 7, 1)) == Decimal("3.00")


class FakeCatalogRepository:
    """In-memory stand-in: these rules are pure logic, not persistence."""

    def __init__(self, service_type: ServiceType, open_price: ServicePrice | None = None) -> None:
        self.service_type = service_type
        self.open_price = open_price
        self.added: list[object] = []
        self.committed = False

    async def get_service_type(self, service_type_id):  # noqa: ANN001, ANN201
        return self.service_type if service_type_id == self.service_type.id else None

    async def get_service_option(self, option_id):  # noqa: ANN001, ANN201
        for option in self.service_type.options:
            if option.id == option_id:
                return option
        return None

    async def get_open_price(self, service_type_id, service_option_id):  # noqa: ANN001, ANN201
        return self.open_price

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.committed = True


def per_unit_service() -> ServiceType:
    service = ServiceType(
        code="wash_by_weight",
        name="Lavado por peso",
        pricing_mode=PricingMode.PER_UNIT,
    )
    service.id = SERVICE_ID
    service.options = []
    return service


def tiered_service() -> ServiceType:
    service = ServiceType(code="wash_tub", name="Lavado por tina", pricing_mode=PricingMode.TIERED)
    service.id = SERVICE_ID
    option = ServiceOption(code="G", name="Tina grande")
    option.id = OPTION_ID
    service.options = [option]
    return service


class TestRegisterPrice:
    async def test_opening_a_price_closes_the_previous_window(self) -> None:
        current = price("2.50", date(2026, 1, 1))
        repository = FakeCatalogRepository(per_unit_service(), open_price=current)
        service = CatalogService(repository)  # type: ignore[arg-type]

        created = await service.register_price(
            SERVICE_ID, ServicePriceCreate(price=Decimal("3.00"), valid_from=date(2026, 7, 1))
        )

        # The old window ends the day before the new one starts: no gap, no overlap.
        assert current.valid_to == date(2026, 6, 30)
        assert created.valid_from == date(2026, 7, 1)
        assert repository.committed is True

    async def test_first_price_needs_no_previous_window(self) -> None:
        repository = FakeCatalogRepository(per_unit_service(), open_price=None)
        service = CatalogService(repository)  # type: ignore[arg-type]

        created = await service.register_price(
            SERVICE_ID, ServicePriceCreate(price=Decimal("2.50"), valid_from=date(2026, 1, 1))
        )

        assert created.price == Decimal("2.50")

    async def test_a_price_cannot_start_before_the_one_in_force(self) -> None:
        """Backdating would leave two windows covering the same day."""
        repository = FakeCatalogRepository(
            per_unit_service(), open_price=price("2.50", date(2026, 7, 1))
        )
        service = CatalogService(repository)  # type: ignore[arg-type]

        with pytest.raises(ConflictError):
            await service.register_price(
                SERVICE_ID, ServicePriceCreate(price=Decimal("3.00"), valid_from=date(2026, 6, 1))
            )

    async def test_tiered_service_requires_an_option(self) -> None:
        repository = FakeCatalogRepository(tiered_service())
        service = CatalogService(repository)  # type: ignore[arg-type]

        with pytest.raises(ConflictError):
            await service.register_price(
                SERVICE_ID, ServicePriceCreate(price=Decimal("30.00"), valid_from=date(2026, 1, 1))
            )

    async def test_per_unit_service_rejects_an_option(self) -> None:
        repository = FakeCatalogRepository(per_unit_service())
        service = CatalogService(repository)  # type: ignore[arg-type]

        with pytest.raises(ConflictError):
            await service.register_price(
                SERVICE_ID,
                ServicePriceCreate(
                    price=Decimal("2.50"), valid_from=date(2026, 1, 1), service_option_id=OPTION_ID
                ),
            )

    async def test_variable_service_has_no_catalog_price(self) -> None:
        """Pickup and delivery cost whatever the courier charges that day."""
        variable = ServiceType(
            code="delivery", name="Entrega a domicilio", pricing_mode=PricingMode.VARIABLE
        )
        variable.id = SERVICE_ID
        variable.options = []
        service = CatalogService(FakeCatalogRepository(variable))  # type: ignore[arg-type]

        with pytest.raises(ConflictError):
            await service.register_price(
                SERVICE_ID, ServicePriceCreate(price=Decimal("25.00"), valid_from=date(2026, 1, 1))
            )

    async def test_option_of_another_service_is_rejected(self) -> None:
        repository = FakeCatalogRepository(tiered_service())
        service = CatalogService(repository)  # type: ignore[arg-type]

        with pytest.raises(NotFoundError):
            await service.register_price(
                SERVICE_ID,
                ServicePriceCreate(
                    price=Decimal("30.00"), valid_from=date(2026, 1, 1), service_option_id=uuid4()
                ),
            )
