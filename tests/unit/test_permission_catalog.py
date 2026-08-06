"""The permission catalog against the code that checks it (Plan 0005 §7, PR 12).

The failure this guards against is silent by construction. Administrators hold
the wildcard, so a permission the server checks but nobody seeded works for
whoever is testing and denies every collaborator with a bare 403 — which is
exactly how `staff.read` and `supply_sales.cancel` were missing for two PRs.

So the checks run in both directions: nothing the source asks for is absent from
the catalog, and nothing in the catalog is dead weight.
"""

import re
from pathlib import Path

import pytest

from src.modules.identity.permissions import CATALOG, CODES, ensure_catalogued
from src.modules.identity.service import WILDCARD_PERMISSION
from src.modules.sync.registry import OPERATION_PERMISSIONS

SRC = Path(__file__).resolve().parents[2] / "src"

#: Every place the server hard-codes a permission: route dependencies and the
#: four checks the orders service makes on its own (a manual discount, editing a
#: ticket that is already ready, delivering one that still owes money).
CHECKS = re.compile(r'(?:require_permission|ensure_permission)\([^)]*?"([^"]+)"')


def sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def checked_codes() -> set[str]:
    found: set[str] = set()
    for path in sources():
        found.update(CHECKS.findall(path.read_text(encoding="utf-8")))
    return found - {WILDCARD_PERMISSION}


class TestTheCatalogCoversTheCode:
    def test_the_scan_finds_what_it_is_meant_to_find(self) -> None:
        """Without this, a broken regex turns the two directions below green.

        The four service-level checks are the ones a route walk would miss, so
        they are named here: they are the reason this reads source text at all.
        """
        found = checked_codes()
        assert {
            "orders.manual_discount",
            "orders.update_ready",
            "orders.deliver_unpaid",
            "daily_close.close",
        } <= found
        assert len(found) == len(CODES)

    def test_every_permission_the_source_checks_is_catalogued(self) -> None:
        missing = sorted(checked_codes() - CODES)
        assert not missing, (
            f"checked but never seeded, so only the wildcard passes them: {missing}"
        )

    def test_every_operation_a_device_may_push_is_catalogued(self) -> None:
        ensure_catalogued(*OPERATION_PERMISSIONS.values())

    def test_the_catalog_has_no_entry_nobody_reads(self) -> None:
        """A permission an administrator can grant and still be told no.

        The reverse drift, and the one that produced `expenses.manage` and
        `supply_sales.manage`: names invented by the seeder before the modules
        existed, granted happily, and checked by nothing.
        """
        referenced = checked_codes() | set(OPERATION_PERMISSIONS.values())
        # `sync.devices.manage` and the three `authorization.*` codes are
        # checked by name like any other; nothing is exempt.
        orphans = sorted(CODES - referenced)
        assert not orphans, f"catalogued but checked nowhere in src/: {orphans}"

    def test_a_code_outside_the_catalog_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expenses.raed"):
            ensure_catalogued("expenses.raed")


class TestWhatTheCollaboratorGets:
    """The right-hand column of Plan 0001 §9 and Plan 0005 §7, spelled out.

    Written as a literal set rather than derived from the catalog on purpose: if
    somebody flips a flag, the diff has to show the permission by name.
    """

    def test_the_collaborator_column_matches_the_plans(self) -> None:
        expected = {
            "attendance.record",
            "catalog.read",
            "customers.create",
            "customers.read",
            "customers.update",
            "daily_close.read",
            "expenses.create",
            "expenses.read",
            "inventory.read",
            "orders.cancel",
            "orders.collect_payment",
            "orders.create",
            "orders.deliver",
            "orders.read",
            "orders.update",
            "promotions.read",
            "staff.read",
            "supply_sales.create",
            "supply_sales.read",
        }
        granted = {spec.code for spec in CATALOG if spec.for_collaborator}
        assert granted == expected

    def test_nothing_administrative_leaks_into_the_collaborator(self) -> None:
        """The four the counter must never hold on its own.

        Closing a day, reopening one, voiding a supply sale and correcting an
        expense are all the same shape of decision: they rewrite what a day
        already said it was worth.
        """
        granted = {spec.code for spec in CATALOG if spec.for_collaborator}
        assert granted.isdisjoint(
            {"daily_close.close", "daily_close.reopen", "supply_sales.cancel", "expenses.update"}
        )


class TestTheCatalogItself:
    def test_codes_are_unique(self) -> None:
        codes = [spec.code for spec in CATALOG]
        assert len(codes) == len(set(codes))

    def test_every_entry_carries_a_description(self) -> None:
        """The description is what the role screen shows next to the checkbox."""
        assert all(spec.description.strip() for spec in CATALOG)

    def test_the_wildcard_is_not_a_catalogued_permission(self) -> None:
        """`*.*` is the fallback `has_permission` reads, not a code to check.

        Seeding it is the seeder's business; a route that asked for it would be
        a route with no permission at all.
        """
        assert WILDCARD_PERMISSION not in CODES
