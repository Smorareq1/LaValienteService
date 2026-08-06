"""Write the permission catalog into the database. Idempotent — safe to re-run.

The list itself lives in `src.modules.identity.permissions`, next to the code
that checks it; this script is only the writer. Existing role/permission links
are preserved: re-running adds what is missing and never revokes what an
administrator granted by hand.

    python -m scripts.seed_permissions
"""

import asyncio

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.modules.identity.models import Permission, Role, RolePermission
from src.modules.identity.permissions import CATALOG, PermissionSpec
from src.modules.identity.service import WILDCARD_PERMISSION

ROLES: tuple[tuple[str, str, str], ...] = (
    ("admin", "Administrador", "Full access to phase-1 operations and administration."),
    ("collaborator", "Colaborador", "Daily counter operation; no catalog or price changes."),
    ("system_admin", "System administrator", "Initial administrative role."),
)

#: Roles that see and may do everything. `system_admin` is created by
#: `scripts.create_superuser` with only the authorization permissions; without the
#: wildcard the very first account of the system sees an almost empty app.
WILDCARD_ROLES = ("admin", "system_admin")


def _catalog_with_wildcard() -> tuple[PermissionSpec, ...]:
    """The catalog plus `*.*`.

    The wildcard is not in the catalog because no route checks it — it is what
    `has_permission` falls back to. The administrative roles still get every
    concrete permission granted explicitly, so that revoking the wildcard some
    day leaves a working administrator instead of a locked-out one.
    """
    resource, action = WILDCARD_PERMISSION.split(".")
    return (*CATALOG, PermissionSpec(resource, action, "Full access to every module", False))


async def seed_permissions() -> None:
    async with AsyncSessionFactory() as session:
        catalog = _catalog_with_wildcard()

        permissions: dict[str, Permission] = {}
        for spec in catalog:
            permission = await session.scalar(
                select(Permission).where(
                    Permission.resource == spec.resource, Permission.action == spec.action
                )
            )
            if permission is None:
                permission = Permission(
                    resource=spec.resource, action=spec.action, description=spec.description
                )
                session.add(permission)
            permissions[spec.code] = permission
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
        for spec in catalog:
            permission = permissions[spec.code]
            targets = [*WILDCARD_ROLES]
            if spec.for_collaborator:
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
