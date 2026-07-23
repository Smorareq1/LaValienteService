"""Create the initial system administrator after migrations have been applied.

Defaults create the first user requested for the project; override with
BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD / BOOTSTRAP_ADMIN_EMAIL /
BOOTSTRAP_ADMIN_PHONE.
"""

import asyncio
import os

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.core.security import hash_password
from src.modules.identity.models import Permission, Role, RolePermission, User, UserRole

ADMIN_PERMISSIONS = (
    ("authorization.permissions", "manage", "Create dynamic permissions"),
    ("authorization.roles", "manage", "Create roles and assign their permissions"),
    ("authorization.users", "manage", "Assign roles and direct permissions to users"),
)


async def create_superuser() -> None:
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "sebasm").strip().lower()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Morales1")
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "smorareq1@gmail.com").strip().lower() or None
    phone = os.environ.get("BOOTSTRAP_ADMIN_PHONE", "+50248152964").strip() or None
    if not username or len(password) < 8:
        raise RuntimeError(
            "Set BOOTSTRAP_ADMIN_USERNAME and an 8+ character BOOTSTRAP_ADMIN_PASSWORD."
        )

    async with AsyncSessionFactory() as session:
        if await session.scalar(select(User).where(User.username == username)):
            raise RuntimeError("A user with the bootstrap username already exists.")

        permissions: list[Permission] = []
        for resource, action, description in ADMIN_PERMISSIONS:
            permission = await session.scalar(
                select(Permission).where(
                    Permission.resource == resource, Permission.action == action
                )
            )
            if permission is None:
                permission = Permission(resource=resource, action=action, description=description)
                session.add(permission)
            permissions.append(permission)
        await session.flush()

        role = await session.scalar(select(Role).where(Role.code == "system_admin"))
        if role is None:
            role = Role(
                code="system_admin",
                name="System administrator",
                description="Initial administrative role.",
                is_system=True,
            )
            session.add(role)
            await session.flush()
            for permission in permissions:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            username=username, email=email, phone=phone, password_hash=hash_password(password)
        )
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.commit()
        print(f"Superuser '{username}' created with role 'system_admin'.")


if __name__ == "__main__":
    asyncio.run(create_superuser())
