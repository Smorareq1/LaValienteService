from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from src.core.exceptions import ConflictError, NotFoundError, StaleVersionError
from src.modules.customers.models import Customer
from src.modules.customers.repository import CustomersRepository
from src.modules.customers.schemas import (
    CustomerCreate,
    CustomerPage,
    CustomerRead,
    CustomerUpdate,
)


class CustomersService:
    """Business rules for customers: soft-delete and duplicate detection."""

    def __init__(self, repository: CustomersRepository) -> None:
        self.repository = repository

    async def search(
        self,
        *,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_inactive: bool = False,
    ) -> CustomerPage:
        items, total = await self.repository.search(
            search=search,
            page=page,
            page_size=page_size,
            include_inactive=include_inactive,
        )
        return CustomerPage(
            items=[CustomerRead.model_validate(customer) for customer in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get(self, customer_id: UUID) -> Customer:
        customer = await self.repository.get(customer_id)
        if customer is None:
            raise NotFoundError("Customer not found.")
        return customer

    async def create(self, data: CustomerCreate) -> tuple[Customer, UUID | None]:
        """Create a customer, returning the id of a likely duplicate if there is one.

        The caller decides what to do with the warning: the REST endpoint surfaces
        it in the response, the sync applier records it on the operation.
        """
        customer, duplicate_of = await self.stage(data)
        await self.repository.commit()
        return customer, duplicate_of

    async def stage(self, data: CustomerCreate) -> tuple[Customer, UUID | None]:
        """Same as :meth:`create` but without committing.

        Taking an order for an unknown customer writes both rows, and they belong
        to one transaction: a customer left behind by a ticket that failed to save
        is a ghost nobody will ever look for.
        """
        if data.id is not None:
            existing = await self.repository.get(data.id)
            if existing is not None:
                raise ConflictError("A customer with that id already exists.")

        duplicate_of: UUID | None = None
        if data.phone:
            duplicate = await self.repository.find_by_phone(data.phone)
            if duplicate is not None:
                duplicate_of = duplicate.id

        payload = data.model_dump(exclude_none=True)
        payload.pop("id", None)
        customer = Customer(**payload)
        if data.id is not None:
            customer.id = data.id

        self.repository.add(customer)
        # Flushed, not just added: `id` comes from a column default that only
        # fires on write, and an order being captured for this customer needs the
        # value right now to point its foreign key at.
        await self.repository.flush()
        return customer, duplicate_of

    async def update(
        self, customer_id: UUID, data: CustomerUpdate, *, base_version: int | None = None
    ) -> Customer:
        customer = await self.get(customer_id)
        self.ensure_version(customer, base_version)

        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(customer, field, value)
        await self.repository.commit()
        return customer

    async def archive(self, customer_id: UUID) -> Customer:
        """Soft-delete: the row stays and travels to devices as a tombstone (D8)."""
        customer = await self.get(customer_id)
        customer.is_active = False
        customer.deleted_at = datetime.now(UTC)
        await self.repository.commit()
        return customer

    @staticmethod
    def ensure_version(customer: Customer, base_version: int | None) -> None:
        """Reject a write built on a version someone else has already superseded.

        `None` means the caller is not doing optimistic concurrency (a plain REST
        edit from the admin screen); sync operations always send one (D6).
        """
        if base_version is None:
            return
        if customer.version != base_version:
            raise StaleVersionError(
                f"The customer changed since version {base_version} "
                f"(current version is {customer.version})."
            )
