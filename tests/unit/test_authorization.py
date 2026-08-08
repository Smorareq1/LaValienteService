"""Who sees what: the wildcard grant and the deny that still beats it."""

from uuid import UUID, uuid4

import pytest

from src.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from src.modules.identity.models import (
    Permission,
    PermissionEffect,
    Role,
    RolePermission,
    User,
    UserPermission,
    UserRole,
)
from src.modules.identity.service import WILDCARD_PERMISSION, IdentityService


def permission(code: str) -> Permission:
    resource, action = code.rsplit(".", 1)
    return Permission(id=uuid4(), resource=resource, action=action)


def user_with(
    *, role_permissions: list[str] | None = None, denied: list[str] | None = None
) -> User:
    """A user carrying `role_permissions` through one role, with `denied` revoked."""
    role_permissions = role_permissions or []
    denied = denied or []
    role = Role(id=uuid4(), code="test_role", name="Test role")
    role.permission_assignments = [
        RolePermission(role_id=role.id, permission=permission(code)) for code in role_permissions
    ]

    # `is_active` is spelled out because it is a *column* default: SQLAlchemy
    # fills it at flush, so an account built in memory would carry `None` and a
    # test about switching it off would be comparing against nothing.
    account = User(id=uuid4(), username="tester", password_hash="x", is_active=True)
    account.role_assignments = [UserRole(user_id=account.id, role=role)]
    account.permission_assignments = [
        UserPermission(
            user_id=account.id, permission=permission(code), effect=PermissionEffect.DENY
        )
        for code in denied
    ]
    return account


def service() -> IdentityService:
    # Permission checks read nothing from the database: they walk the graph the
    # session already loaded, so a repository is not needed here.
    return IdentityService.__new__(IdentityService)


class TestWildcard:
    def test_the_wildcard_grants_a_permission_nobody_assigned(self) -> None:
        """An admin must not lose a module the day it ships."""
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])

        assert service().has_permission(admin, "daily_close.close")

    def test_a_denial_beats_the_wildcard(self) -> None:
        admin = user_with(role_permissions=[WILDCARD_PERMISSION], denied=["daily_close.reopen"])

        assert not service().has_permission(admin, "daily_close.reopen")
        assert service().has_permission(admin, "daily_close.close")

    def test_without_the_wildcard_only_what_was_granted_counts(self) -> None:
        collaborator = user_with(role_permissions=["orders.read"])

        assert service().has_permission(collaborator, "orders.read")
        assert not service().has_permission(collaborator, "catalog.manage")

    def test_ensure_permission_raises_for_what_has_permission_denies(self) -> None:
        collaborator = user_with(role_permissions=["orders.read"])

        with pytest.raises(AuthorizationError):
            service().ensure_permission(collaborator, "catalog.manage")


class TestCurrentUserView:
    def test_denials_travel_to_the_client(self) -> None:
        """The app hides a section using the same two lists the API enforces."""
        admin = user_with(role_permissions=[WILDCARD_PERMISSION], denied=["daily_close.reopen"])

        view = service().current_user_view(admin)

        assert WILDCARD_PERMISSION in view.permissions
        assert view.denied_permissions == ["daily_close.reopen"]

    def test_a_denied_code_never_appears_as_granted(self) -> None:
        account = user_with(
            role_permissions=["orders.read", "orders.cancel"], denied=["orders.cancel"]
        )

        view = service().current_user_view(account)

        assert view.permissions == ["orders.read"]


class FakeIdentityRepository:
    """Just enough of the repository for the on/off switch (Plan 0006 §12).

    Accounts are looked up by id and the two side effects that matter — the
    commit and the session revocation — are recorded rather than performed.
    """

    def __init__(self, *users: User) -> None:
        self.users = {account.id: account for account in users}
        self.revoked_for: list[UUID] = []
        self.commits = 0

    async def get_user_with_access(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def revoke_all_sessions(self, user_id: UUID) -> None:
        self.revoked_for.append(user_id)

    async def commit(self) -> None:
        self.commits += 1


def service_over(repository: FakeIdentityRepository) -> IdentityService:
    account_service = IdentityService.__new__(IdentityService)
    account_service.repository = repository  # type: ignore[assignment]
    return account_service


class TestTurningAnAccountOff:
    """`PATCH /authorization/users/{id}` — the only writer of `is_active`."""

    async def test_an_account_is_switched_off_and_its_sessions_revoked(self) -> None:
        """A live refresh token must not outlive the decision."""
        target = user_with(role_permissions=["orders.read"])
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])
        repository = FakeIdentityRepository(target, admin)

        view = await service_over(repository).set_user_active(admin, target.id, False)

        assert view.is_active is False
        assert target.is_active is False
        assert repository.revoked_for == [target.id]
        assert repository.commits == 1

    async def test_switching_one_back_on_leaves_the_sessions_alone(self) -> None:
        """Turning the account on must not hand back the sessions it lost."""
        target = user_with(role_permissions=["orders.read"])
        target.is_active = False
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])
        repository = FakeIdentityRepository(target, admin)

        view = await service_over(repository).set_user_active(admin, target.id, True)

        assert view.is_active is True
        assert repository.revoked_for == []

    async def test_an_account_cannot_switch_itself_off(self) -> None:
        """Otherwise the only way back is the script this screen replaces."""
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])
        repository = FakeIdentityRepository(admin)

        with pytest.raises(ConflictError):
            await service_over(repository).set_user_active(admin, admin.id, False)

        assert admin.is_active is True
        assert repository.commits == 0

    async def test_an_account_may_switch_itself_on(self) -> None:
        """Nonsense in practice — an inactive account cannot authenticate — but
        the guard is about lockout, so it only covers the direction that locks."""
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])
        repository = FakeIdentityRepository(admin)

        view = await service_over(repository).set_user_active(admin, admin.id, True)

        assert view.is_active is True

    async def test_an_unknown_account_is_not_found(self) -> None:
        admin = user_with(role_permissions=[WILDCARD_PERMISSION])
        repository = FakeIdentityRepository(admin)

        with pytest.raises(NotFoundError):
            await service_over(repository).set_user_active(admin, uuid4(), False)
