"""Who sees what: the wildcard grant and the deny that still beats it."""

from uuid import uuid4

import pytest

from src.core.exceptions import AuthorizationError
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

    account = User(id=uuid4(), username="tester", password_hash="x")
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
