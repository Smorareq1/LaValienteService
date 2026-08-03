"""Seed the operational RBAC of Plan 0001 §9. Idempotent — safe to re-run.

Creates the `admin` and `collaborator` roles and the permissions the phase-1
modules check. Existing role/permission links are preserved: re-running adds what
is missing and never revokes what an administrator granted by hand.

    python -m scripts.seed_permissions
"""

import asyncio

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.modules.identity.models import Permission, Role, RolePermission
from src.modules.identity.service import WILDCARD_PERMISSION

#: (resource, action, description, granted to collaborator?)
#: The `admin` role does not need entries here — it holds the wildcard — but they
#: are still granted explicitly so that revoking the wildcard some day leaves a
#: working administrator instead of a locked-out one.
PERMISSIONS: tuple[tuple[str, str, str, bool], ...] = (
    ("customers", "read", "Search and view customers", True),
    ("customers", "create", "Register a new customer", True),
    ("customers", "update", "Edit a customer's details", True),
    ("customers", "archive", "Archive a customer", False),
    ("catalog", "read", "Read services, options, prices and garment types", True),
    ("catalog", "manage", "Administer the catalog and its prices", False),
    ("orders", "create", "Take an order", True),
    ("orders", "read", "List and view orders", True),
    ("orders", "update", "Edit an order and advance its status", True),
    ("orders", "deliver", "Hand an order back to the customer", True),
    ("orders", "cancel", "Void an order", True),
    ("orders", "collect_payment", "Register a payment", True),
    ("orders", "manual_discount", "Apply a discount with no promotion behind it", False),
    ("orders", "deliver_unpaid", "Deliver an order that still owes money", False),
    ("promotions", "read", "See the promotions in force", True),
    ("promotions", "manage", "Administer promotions", False),
    # Plan 0005 §7. The modules ship later, but the app already gates its sections
    # on these codes: without them here, the role/screen matrix of Plan 0006 §13
    # cannot be expressed and a collaborator would simply see nothing.
    ("expenses", "read", "See the day's expenses", True),
    ("expenses", "create", "Record an expense", True),
    ("expenses", "manage", "Edit or void an expense", False),
    ("supply_sales", "create", "Sell a supply over the counter", True),
    ("supply_sales", "manage", "Cancel a supply sale", False),
    ("daily_close", "read", "See the daily close record", False),
    ("daily_close", "close", "Close the day", False),
    ("daily_close", "reopen", "Reopen a closed day", False),
    ("inventory", "read", "See products, batches and stock", True),
    ("inventory", "manage", "Administer products and batches", False),
    ("inventory", "adjust", "Adjust stock by hand", False),
    ("attendance", "record", "Clock in and out", True),
    ("staff", "read", "See employees, shifts and rates", False),
    ("staff", "manage", "Administer employees, shifts and rates", False),
    # Not in the plan's table: device revocation (Plan 0004 D11) needs a
    # permission of its own and it is squarely an administrator's call.
    ("sync.devices", "manage", "List and revoke synchronization devices", False),
    ("authorization.permissions", "manage", "Create dynamic permissions", False),
    ("authorization.roles", "manage", "Create roles and assign their permissions", False),
    ("authorization.users", "manage", "Assign roles and direct permissions to users", False),
)

ROLES: tuple[tuple[str, str, str], ...] = (
    ("admin", "Administrador", "Full access to phase-1 operations and administration."),
    ("collaborator", "Colaborador", "Daily counter operation; no catalog or price changes."),
    ("system_admin", "System administrator", "Initial administrative role."),
)

#: Roles that see and may do everything. `system_admin` is created by
#: `scripts.create_superuser` with only the authorization permissions; without the
#: wildcard the very first account of the system sees an almost empty app.
WILDCARD_ROLES = ("admin", "system_admin")


async def seed_permissions() -> None:
    async with AsyncSessionFactory() as session:
        wildcard_resource, wildcard_action = WILDCARD_PERMISSION.split(".")
        catalog: tuple[tuple[str, str, str, bool], ...] = (
            *PERMISSIONS,
            (wildcard_resource, wildcard_action, "Full access to every module", False),
        )

        permissions: dict[tuple[str, str], Permission] = {}
        for resource, action, description, _ in catalog:
            permission = await session.scalar(
                select(Permission).where(
                    Permission.resource == resource, Permission.action == action
                )
            )
            if permission is None:
                permission = Permission(resource=resource, action=action, description=description)
                session.add(permission)
            permissions[(resource, action)] = permission
        await session.flush()

        roles: dict[str, Role] = {}
        for code, name, description in ROLES:
            role = await session.scalar(select(Role).where(Role.code == code))
            if role is None:
                role = Role(code=code, name=name, description=description, is_system=True)
                session.add(role)
            roles[code] = role
        await session.flush()

        granted = 0
        for resource, action, _, for_collaborator in catalog:
            permission = permissions[(resource, action)]
            targets = [*WILDCARD_ROLES]
            if for_collaborator:
                targets.append("collaborator")
            for role_code in targets:
                role = roles[role_code]
                existing = await session.scalar(
                    select(RolePermission).where(
                        RolePermission.role_id == role.id,
                        RolePermission.permission_id == permission.id,
                    )
                )
                if existing is None:
                    session.add(RolePermission(role_id=role.id, permission_id=permission.id))
                    granted += 1

        await session.commit()
        print(
            f"Seeded {len(catalog)} permissions and {len(ROLES)} roles "
            f"({granted} new role-permission links)."
        )


if __name__ == "__main__":
    asyncio.run(seed_permissions())
