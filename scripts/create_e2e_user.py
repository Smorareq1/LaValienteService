"""Create (or reset) the end-to-end test user with the seeded `admin` role.

Development fixture only: it exists so the integration tests never touch a real
person's account. Idempotent — running it twice just resets the password.
"""

import asyncio
import os

from sqlalchemy import select

from src.core.database import AsyncSessionFactory
from src.core.security import hash_password
from src.modules.identity.models import Role, User, UserRole

USERNAME = os.environ.get("E2E_USER", "e2e_tester")
PASSWORD = os.environ.get("E2E_PASSWORD", "")


async def main() -> None:
    if len(PASSWORD) < 8:
        raise SystemExit("Set E2E_PASSWORD to at least 8 characters.")

    async with AsyncSessionFactory() as session:
        role = await session.scalar(select(Role).where(Role.code == "admin"))
        if role is None:
            raise SystemExit("Run scripts.seed_permissions first: the 'admin' role is missing.")

        user = await session.scalar(select(User).where(User.username == USERNAME))
        if user is None:
            user = User(
                username=USERNAME,
                full_name="Usuario de pruebas E2E",
                password_hash=hash_password(PASSWORD),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            created = True
        else:
            user.password_hash = hash_password(PASSWORD)
            created = False

        already_assigned = await session.scalar(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        )
        if already_assigned is None:
            session.add(UserRole(user_id=user.id, role_id=role.id))

        await session.commit()
        print(f"{'Creado' if created else 'Actualizado'}: {USERNAME} con rol admin")


asyncio.run(main())
