"""Create the initial system administrator after migrations have been applied."""

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
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or len(password) < 12:
        raise RuntimeError(
            "Set BOOTSTRAP_ADMIN_EMAIL and a 12+ character BOOTSTRAP_ADMIN_PASSWORD."
        )

    async with AsyncSessionFactory() as session:
        if await session.scalar(select(User).where(User.email == email)):
            raise RuntimeError("A user with the bootstrap email already exists.")

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
            role.permission_assignments = [
                RolePermission(permission=permission) for permission in permissions
            ]

        user = User(email=email, password_hash=hash_password(password))
        user.role_assignments = [UserRole(role=role)]
        session.add(user)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(create_superuser())
