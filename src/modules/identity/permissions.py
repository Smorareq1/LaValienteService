"""The permission catalog of phase 1: every code the API checks, in one place.

This list used to live in `scripts/seed_permissions.py`, which meant the server
decided what to check and a script decided what could be granted — two lists that
had already drifted apart three times (Plan 0005, notes (f) of PR 7 and (i) of
PR 8). The drift is quiet in the worst possible way: an administrator holds the
wildcard and passes any check, so a code the seeder never created works perfectly
for whoever is testing and denies the collaborator forever.

So the catalog is here, in `src`, and:

* `scripts.seed_permissions` writes exactly what this file says;
* `require_permission` refuses to build a route around a code that is not here,
  which turns the typo into an import error instead of a 403 nobody sees.

Permissions an administrator creates at runtime (`authorization.permissions`) are
deliberately *not* constrained by this: the catalog is what the server's own code
checks, not the whole of what the database may hold.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """One permission, and whether a collaborator gets it by default."""

    resource: str
    action: str
    description: str
    #: The default composition of the `collaborator` role. It is a *seed*, not a
    #: rule: the seeder never revokes, so an administrator who grants or denies
    #: something by hand keeps that decision across re-runs.
    for_collaborator: bool

    @property
    def code(self) -> str:
        return f"{self.resource}.{self.action}"


#: Plan 0001 §9 and Plan 0005 §7, in one accumulative table.
CATALOG: tuple[PermissionSpec, ...] = (
    PermissionSpec("customers", "read", "Search and view customers", True),
    PermissionSpec("customers", "create", "Register a new customer", True),
    PermissionSpec("customers", "update", "Edit a customer's details", True),
    PermissionSpec("customers", "archive", "Archive a customer", False),
    PermissionSpec("catalog", "read", "Read services, options, prices and garment types", True),
    PermissionSpec("catalog", "manage", "Administer the catalog and its prices", False),
    PermissionSpec("orders", "create", "Take an order", True),
    PermissionSpec("orders", "read", "List and view orders", True),
    PermissionSpec("orders", "update", "Edit an order and advance its status", True),
    # §7.3 draws a line the permission table of §9 never named: a collaborator
    # edits a ticket while it is `received` or `in_progress`, but one that is
    # already `ready` has been washed and folded, and rewriting it there is an
    # administrator's call.
    PermissionSpec("orders", "update_ready", "Edit an order that is already ready", False),
    PermissionSpec("orders", "deliver", "Hand an order back to the customer", True),
    PermissionSpec("orders", "cancel", "Void an order", True),
    PermissionSpec("orders", "collect_payment", "Register a payment", True),
    PermissionSpec(
        "orders", "manual_discount", "Apply a discount with no promotion behind it", False
    ),
    PermissionSpec("orders", "deliver_unpaid", "Deliver an order that still owes money", False),
    PermissionSpec("promotions", "read", "See the promotions in force", True),
    PermissionSpec("promotions", "manage", "Administer promotions", False),
    # Plan 0005 §7.
    PermissionSpec("expenses", "read", "See the day's expenses", True),
    PermissionSpec("expenses", "create", "Record an expense", True),
    PermissionSpec("expenses", "update", "Edit an expense", False),
    PermissionSpec("expenses", "void", "Void an expense", False),
    PermissionSpec("expenses", "manage_categories", "Administer expense categories", False),
    PermissionSpec("supply_sales", "create", "Sell a supply over the counter", True),
    PermissionSpec("supply_sales", "read", "See the counter sales of supplies", True),
    PermissionSpec("supply_sales", "cancel", "Void a supply sale", False),
    # §7 gives the collaborator the day's numbers and only the administrator the
    # act of closing: the close screen of Plan 0006 is the paper sheet on screen,
    # and a counter that cannot see the day's total cannot check the drawer
    # against it.
    PermissionSpec("daily_close", "read", "See the daily close record", True),
    PermissionSpec("daily_close", "close", "Close the day", False),
    PermissionSpec("daily_close", "reopen", "Reopen a closed day", False),
    PermissionSpec("inventory", "read", "See products, batches and stock", True),
    PermissionSpec("inventory", "manage", "Administer products and batches", False),
    PermissionSpec("inventory", "adjust", "Adjust stock by hand", False),
    PermissionSpec("attendance", "record", "Clock in and out", True),
    # The attendance screen (Plan 0006 §9.1) is a list of employees with a button
    # on each card: without `staff.read` a collaborator who may clock people in
    # has nobody to clock in. Rates stay administrative — `GET /staff/rates` asks
    # for `staff.manage`, not for this.
    PermissionSpec("staff", "read", "See employees, shifts and the day's attendance", True),
    PermissionSpec("staff", "manage", "Administer employees, shifts and rates", False),
    # Plan 0003 §8. The scan is the accessibility module of *everyone* — it is
    # how someone who types slowly takes an order — so the collaborator holds
    # both. They are the only codes here belonging to a module that is meant to
    # be deleted: the day the shop drops the paper booklet, these two go with it.
    PermissionSpec("scans", "create", "Scan a paper ticket to draft an order", True),
    PermissionSpec("scans", "read", "Look up a scan and its photo", True),
    # The daily sheet (Plan 0005 §1) read through the same module. Reading it is
    # the collaborator's — it is their sheet — but **importing** it is not: that
    # one act collects a whole day's money and hands out a whole day's clothes,
    # and the permissions it borrows underneath (`orders.collect_payment`,
    # `orders.deliver`, `expenses.create`) are each granted for one ticket at a
    # time in front of the customer it belongs to. Fifteen at once off a
    # photograph is a different decision, so it gets a gate of its own.
    PermissionSpec("scans", "import_close", "Import a scanned daily sheet", False),
    # Not in the plans' tables: device revocation (Plan 0004 D11) needs a
    # permission of its own and it is squarely an administrator's call.
    PermissionSpec("sync.devices", "manage", "List and revoke synchronization devices", False),
    PermissionSpec("authorization.permissions", "manage", "Create dynamic permissions", False),
    PermissionSpec(
        "authorization.roles", "manage", "Create roles and assign their permissions", False
    ),
    PermissionSpec(
        "authorization.users", "manage", "Assign roles and direct permissions to users", False
    ),
)

CODES: frozenset[str] = frozenset(spec.code for spec in CATALOG)


def ensure_catalogued(*codes: str) -> None:
    """Raise unless every code given is one this file defines.

    Called where the server hard-codes a permission — route dependencies and the
    sync operation table — so a misspelled code stops the process at import,
    loudly, instead of quietly locking out every account without the wildcard.
    """
    unknown = sorted({code for code in codes if code not in CODES})
    if unknown:
        raise ValueError(
            f"Permission code(s) not in the catalog: {', '.join(unknown)}. "
            "Add them to src/modules/identity/permissions.py and re-run "
            "`python -m scripts.seed_permissions`."
        )
