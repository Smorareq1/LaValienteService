from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from src.core.exceptions import (
    AuthorizationError,
    DomainError,
    NotFoundError,
    StaleVersionError,
)
from src.modules.customers.schemas import CustomerCreate, CustomerUpdate
from src.modules.customers.service import CustomersService
from src.modules.expenses.schemas import ExpenseCreate, ExpenseUpdate
from src.modules.expenses.service import ExpensesService
from src.modules.identity.models import User
from src.modules.identity.service import IdentityService
from src.modules.inventory.allocation import InsufficientStock
from src.modules.inventory.schemas import SupplySaleCancel, SupplySaleCreate
from src.modules.inventory.service import InventoryService
from src.modules.orders.schemas import (
    OrderCancel,
    OrderCreate,
    OrderDeliver,
    OrderPaymentPush,
    OrderStatusChange,
    OrderUpdate,
)
from src.modules.orders.service import OrdersService
from src.modules.staff.schemas import AttendanceCreate, AttendanceUpdate
from src.modules.staff.service import StaffService
from src.modules.sync.models import OperationStatus, SyncDevice, SyncOperation
from src.modules.sync.registry import FEED_ENTITIES_BY_NAME, OPERATION_PERMISSIONS
from src.modules.sync.repository import SyncRepository
from src.modules.sync.schemas import (
    MAX_PULL_PAGE_SIZE,
    DeviceRegister,
    SyncChange,
    SyncOperationIn,
    SyncOperationResult,
    SyncPullResponse,
    SyncPushResponse,
)

logger = logging.getLogger(__name__)

WIPE_DIRECTIVE = "wipe"

#: How far a device clock may drift before it is worth telling anyone.
CLOCK_SKEW_WARNING_SECONDS = 300


class SyncService:
    """Push/pull protocol of Plan 0004 §7.

    The rule that shapes everything here: an operation is a *command*, replayed
    through the same domain services the REST API uses (D2). Sync is a second
    transport, never a back door around business rules.
    """

    def __init__(
        self,
        repository: SyncRepository,
        customers: CustomersService,
        orders: OrdersService,
        identity: IdentityService,
        staff: StaffService,
        expenses: ExpensesService,
        inventory: InventoryService,
    ) -> None:
        self.repository = repository
        self.customers = customers
        self.orders = orders
        self.identity = identity
        self.staff = staff
        self.expenses = expenses
        self.inventory = inventory

    # -- devices ---------------------------------------------------------

    async def register_device(self, user: User, data: DeviceRegister) -> SyncDevice:
        """Register an installation, or re-bind an existing one to this user."""
        device = await self.repository.get_device(data.id)
        now = datetime.now(UTC)

        if device is None:
            device = SyncDevice(
                id=data.id,
                user_id=user.id,
                name=data.name,
                platform=data.platform,
                app_version=data.app_version,
                last_seen_at=now,
            )
            self.repository.add(device)
        else:
            if device.is_revoked:
                raise AuthorizationError("This device was revoked; contact an administrator.")
            device.user_id = user.id
            device.name = data.name
            device.platform = data.platform
            device.app_version = data.app_version
            device.last_seen_at = now

        await self.repository.commit()
        return device

    async def list_devices(self) -> list[SyncDevice]:
        return await self.repository.list_devices()

    async def revoke_device(self, device_id: UUID) -> SyncDevice:
        device = await self._require_device(device_id)
        if not device.is_revoked:
            device.revoked_at = datetime.now(UTC)
            await self.repository.commit()
        return device

    async def _require_device(self, device_id: UUID) -> SyncDevice:
        device = await self.repository.get_device(device_id)
        if device is None:
            raise NotFoundError("Device is not registered.")
        return device

    # -- push ------------------------------------------------------------

    async def push(
        self, user: User, device_id: UUID, operations: list[SyncOperationIn]
    ) -> SyncPushResponse:
        """Apply a batch of operations, one transaction each.

        Per Plan 0004 §7.1 a single bad operation must not sink the batch, so each
        one commits or rolls back on its own and reports its own outcome.
        """
        device = await self._require_device(device_id)
        now = datetime.now(UTC)

        if device.is_revoked:
            # Nothing from a revoked device is applied; it is told to wipe instead.
            return SyncPushResponse(
                results=[],
                device_directive=WIPE_DIRECTIVE,
                server_time=now,
            )

        results: list[SyncOperationResult] = []
        for operation in sorted(operations, key=lambda item: item.seq):
            results.append(await self._apply_one(user, device, operation))

        device.last_push_at = now
        device.last_seen_at = now
        await self.repository.commit()

        return SyncPushResponse(results=results, device_directive=None, server_time=now)

    async def _apply_one(
        self, user: User, device: SyncDevice, operation: SyncOperationIn
    ) -> SyncOperationResult:
        recorded = await self.repository.get_operation(operation.op_id)
        if recorded is not None:
            return self._replay(recorded)

        self._warn_on_clock_skew(device, operation)

        try:
            self._authorize(user, operation)
            server_version, server_data, warnings = await self._dispatch(user, operation)
        except StaleVersionError as error:
            # The only outcome the plan calls a conflict (Plan 0004 §8): the row
            # moved while the device was away, and the two versions have to be put
            # side by side for a person to choose. Every other refusal below is a
            # rule that said no, which is a different screen and a different fix.
            return await self._record(
                device, user, operation, OperationStatus.CONFLICT, reason=str(error)
            )
        except InsufficientStock as error:
            # D12: the counter has to see both sides, so the shelf as the server
            # sees it now travels with the rejection instead of only in the text.
            return await self._record(
                device,
                user,
                operation,
                OperationStatus.REJECTED,
                reason=str(error),
                server_data={
                    "product_name": error.product_name,
                    "requested": str(error.requested),
                    "available": str(error.available),
                },
            )
        except (DomainError, ValidationError, ValueError) as error:
            return await self._record(
                device, user, operation, OperationStatus.REJECTED, reason=str(error)
            )

        return await self._record(
            device,
            user,
            operation,
            OperationStatus.APPLIED,
            server_version=server_version,
            server_data=server_data,
            warnings=warnings,
        )

    @staticmethod
    def _replay(recorded: SyncOperation) -> SyncOperationResult:
        """Re-serve a stored outcome. This is what makes retries free (D4)."""
        stored = recorded.result or {}
        status = (
            OperationStatus.ALREADY_APPLIED
            if recorded.status is OperationStatus.APPLIED
            else recorded.status
        )
        return SyncOperationResult(
            op_id=recorded.op_id,
            status=status,
            entity_id=recorded.entity_id,
            server_version=stored.get("version"),
            server_data=stored or None,
            warnings=list(stored.get("warnings", [])),
            reason=recorded.reason,
        )

    @staticmethod
    def _warn_on_clock_skew(device: SyncDevice, operation: SyncOperationIn) -> None:
        if operation.client_ts is None:
            return

        # A client may send a timestamp without an offset. Assuming UTC keeps a
        # diagnostic warning from taking down the whole push with a TypeError.
        client_ts = operation.client_ts
        if client_ts.tzinfo is None:
            client_ts = client_ts.replace(tzinfo=UTC)

        skew = abs((datetime.now(UTC) - client_ts).total_seconds())
        if skew > CLOCK_SKEW_WARNING_SECONDS:
            logger.warning(
                "sync.clock_skew device_id=%s op_id=%s skew_seconds=%.0f",
                device.id,
                operation.op_id,
                skew,
            )

    def _authorize(self, user: User, operation: SyncOperationIn) -> None:
        key = (operation.entity, operation.op_type)
        permission = OPERATION_PERMISSIONS.get(key)
        if permission is None:
            raise NotFoundError(
                f"Operation '{operation.op_type}' on '{operation.entity}' is not supported."
            )
        self.identity.ensure_permission(user, permission)

    async def _dispatch(
        self, user: User, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Route the command to its domain service and report what was stored."""
        match operation.entity:
            case "customer":
                return await self._apply_customer(operation)
            case "order" | "order_payment":
                return await self._apply_order(user, operation)
            case "attendance_record":
                return await self._apply_attendance(operation)
            case "expense":
                return await self._apply_expense(user, operation)
            case "supply_sale":
                return await self._apply_supply_sale(user, operation)
            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(f"Entity '{operation.entity}' is not supported.")

    async def _apply_order(
        self, user: User, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Replay a life-cycle command through the same service the API uses (D2).

        The server recomputes the total here just as it would for an HTTP request,
        so a device that captured a ticket while the price list changed gets the
        corrected figures back in `server_data` rather than keeping its own.
        """
        match (operation.entity, operation.op_type):
            case ("order", "create"):
                creation = OrderCreate.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                order, warnings = await self.orders.create(creation, actor=user)
                return order.version, self._serialize("order", order), warnings

            case ("order", "update"):
                changes = OrderUpdate.model_validate(
                    {**operation.payload, "base_version": operation.base_version}
                )
                order, warnings = await self.orders.update(
                    operation.entity_id, changes, actor=user
                )
                return order.version, self._serialize("order", order), warnings

            case ("order", "status"):
                change = OrderStatusChange.model_validate(operation.payload)
                order = await self.orders.change_status(
                    operation.entity_id, change.status, actor=user
                )
                return order.version, self._serialize("order", order), []

            case ("order", "deliver"):
                delivery = OrderDeliver.model_validate(operation.payload)
                order = await self.orders.deliver(operation.entity_id, delivery, actor=user)
                return order.version, self._serialize("order", order), []

            case ("order", "cancel"):
                cancellation = OrderCancel.model_validate(operation.payload)
                order = await self.orders.cancel(operation.entity_id, cancellation, actor=user)
                return order.version, self._serialize("order", order), []

            case ("order_payment", "create"):
                push = OrderPaymentPush.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                order = await self.orders.add_payment(push.order_id, push, actor=user)
                payment = next(
                    row for row in order.payments if row.id == operation.entity_id
                )
                return payment.version, self._serialize("order_payment", payment), []

            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(
                    f"Operation '{operation.op_type}' on '{operation.entity}' is not supported."
                )

    async def _apply_customer(
        self, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        match (operation.entity, operation.op_type):
            case ("customer", "create"):
                creation = CustomerCreate.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                customer, duplicate_of = await self.customers.create(creation)
                warnings = (
                    [f"possible_duplicate_of:{duplicate_of}"] if duplicate_of is not None else []
                )
                return customer.version, self._serialize("customer", customer), warnings

            case ("customer", "update"):
                changes = CustomerUpdate.model_validate(operation.payload)
                customer = await self.customers.update(
                    operation.entity_id, changes, base_version=operation.base_version
                )
                return customer.version, self._serialize("customer", customer), []

            case ("customer", "archive"):
                # No `base_version` check: archiving is a state transition, not a
                # field write. Doing it on top of someone else's rename is not a
                # conflict — the customer is retired either way.
                customer = await self.customers.archive(operation.entity_id)
                return customer.version, self._serialize("customer", customer), []

            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(
                    f"Operation '{operation.op_type}' on '{operation.entity}' is not supported."
                )

    async def _apply_attendance(
        self, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Clocking in and out from a device (Plan 0005 §6.4).

        The row is re-read after the write rather than serialized from what the
        service returned: `AttendanceRead` carries the *suggested* overtime, and
        the feed must never hand a device a number that looks like a decision
        somebody made (D8).
        """
        match operation.op_type:
            case "create":
                creation = AttendanceCreate.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                await self.staff.clock_in(creation)

            case "update":
                changes = AttendanceUpdate.model_validate(operation.payload)
                await self.staff.update_attendance(
                    operation.entity_id, changes, base_version=operation.base_version
                )

            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(
                    f"Operation '{operation.op_type}' on 'attendance_record' is not supported."
                )

        record = await self.staff.get_attendance(operation.entity_id)
        return record.version, self._serialize("attendance_record", record), []

    async def _apply_expense(
        self, user: User, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        """An expense captured at the counter, with or without signal (§6.4).

        A closed day refuses it here exactly as it would over HTTP: the guard is
        in the domain service, so this transport gets it without asking.
        """
        match operation.op_type:
            case "create":
                creation = ExpenseCreate.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                await self.expenses.create_expense(creation, actor=user)

            case "update":
                changes = ExpenseUpdate.model_validate(operation.payload)
                await self.expenses.update_expense(
                    operation.entity_id, changes, base_version=operation.base_version
                )

            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(
                    f"Operation '{operation.op_type}' on 'expense' is not supported."
                )

        expense = await self.expenses.get_expense(operation.entity_id)
        return expense.version, self._serialize("expense", expense), []

    async def _apply_supply_sale(
        self, user: User, operation: SyncOperationIn
    ) -> tuple[int, dict[str, Any], list[str]]:
        """A counter sale replayed against the shelf as it stands now.

        The device sent products and quantities; which lots cover them and what
        they cost is recomputed here (D10 of Plan 0004). A sale captured while
        the price list moved comes back repriced in `server_data`, and one the
        stock can no longer cover comes back rejected with the shelf attached
        (D12) — see the handler in `_apply_one`.
        """
        match operation.op_type:
            case "create":
                creation = SupplySaleCreate.model_validate(
                    {**operation.payload, "id": operation.entity_id}
                )
                sale = await self.inventory.create_sale(creation, actor=user)

            case "cancel":
                cancellation = SupplySaleCancel.model_validate(operation.payload)
                sale = await self.inventory.cancel_sale(
                    operation.entity_id, cancellation, actor=user
                )

            case _:  # pragma: no cover — _authorize already rejected these
                raise NotFoundError(
                    f"Operation '{operation.op_type}' on 'supply_sale' is not supported."
                )

        return sale.version, self._serialize("supply_sale", sale), []

    @staticmethod
    def _serialize(entity_name: str, row: object) -> dict[str, Any]:
        return FEED_ENTITIES_BY_NAME[entity_name].serialize(row)

    async def _record(
        self,
        device: SyncDevice,
        user: User,
        operation: SyncOperationIn,
        status: OperationStatus,
        *,
        server_version: int | None = None,
        server_data: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        reason: str | None = None,
    ) -> SyncOperationResult:
        """Write the receipt and return the result for this operation."""
        result_payload = dict(server_data or {})
        if warnings:
            result_payload["warnings"] = warnings

        self.repository.add(
            SyncOperation(
                op_id=operation.op_id,
                device_id=device.id,
                user_id=user.id,
                entity=operation.entity,
                op_type=operation.op_type,
                entity_id=operation.entity_id,
                payload=operation.payload,
                status=status,
                result=result_payload or None,
                reason=reason,
                client_ts=operation.client_ts,
            )
        )
        await self.repository.commit()

        return SyncOperationResult(
            op_id=operation.op_id,
            status=status,
            entity_id=operation.entity_id,
            server_version=server_version,
            server_data=server_data,
            warnings=warnings or [],
            reason=reason,
        )

    # -- pull ------------------------------------------------------------

    async def pull(
        self, device_id: UUID, cursor: int, page_size: int = MAX_PULL_PAGE_SIZE
    ) -> SyncPullResponse:
        """Everything that changed after `cursor`, in global sequence order (D7)."""
        device = await self._require_device(device_id)
        now = datetime.now(UTC)

        if device.is_revoked:
            return SyncPullResponse(
                changes=[],
                next_cursor=cursor,
                has_more=False,
                server_time=now,
                device_directive=WIPE_DIRECTIVE,
            )

        page_size = min(page_size, MAX_PULL_PAGE_SIZE)
        collected = await self.repository.fetch_all_changes(cursor, page_size)
        has_more = len(collected) > page_size
        page = collected[:page_size]

        changes = [
            SyncChange(
                entity=entity.name,
                id=row.id,
                version=row.version,
                sync_seq=row.sync_seq,
                deleted=row.deleted_at is not None,
                data=entity.serialize(row),
            )
            for entity, row in page
        ]

        next_cursor = changes[-1].sync_seq if changes else cursor

        # The cursor is recorded for observability only; the device owns the real
        # one and persists it after applying the page (§7.2).
        device.last_pull_cursor = next_cursor
        device.last_seen_at = now
        await self.repository.commit()

        return SyncPullResponse(
            changes=changes,
            next_cursor=next_cursor,
            has_more=has_more,
            server_time=now,
            device_directive=None,
        )
