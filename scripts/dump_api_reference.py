"""Rewrite the route table of `docs/API.md` from the running application.

A reference that lists routes by hand is a reference that lies within a month.
This reads the mounted app instead: the method, the path and the permission each
route actually enforces, in the order the routers are included.

    python -m scripts.dump_api_reference

`tests/unit/test_api_reference.py` runs the same rendering and compares it with
the committed file, so adding a route without regenerating fails the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from src.api.dependencies import PERMISSION_BY_DEPENDENCY, get_current_user
from src.main import app

DOC = Path(__file__).resolve().parents[1] / "docs" / "API.md"
START = "<!-- rutas: generado por scripts/dump_api_reference.py -->"
END = "<!-- fin rutas -->"

#: What the permission column says when a route has no permission gate.
PUBLIC = "—"
SESSION = "sesión"


def _walk(dependant: Dependant) -> list[Dependant]:
    found = [dependant]
    for child in dependant.dependencies:
        found.extend(_walk(child))
    return found


def _permission(route: APIRoute) -> str:
    """The code this route enforces, or how it is gated when there is none."""
    for dependant in _walk(route.dependant):
        if dependant.call is not None and dependant.call in PERMISSION_BY_DEPENDENCY:
            return f"`{PERMISSION_BY_DEPENDENCY[dependant.call]}`"
    authenticated = any(
        dependant.call is get_current_user for dependant in _walk(route.dependant)
    )
    return SESSION if authenticated else PUBLIC


def render() -> str:
    """The whole generated region, markers included."""
    groups: dict[str, list[str]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        tag = str(route.tags[0]) if route.tags else "Sin grupo"
        methods = ", ".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
        groups.setdefault(tag, []).append(
            f"| `{methods}` | `{route.path}` | {_permission(route)} |"
        )

    lines = [START, ""]
    for tag, rows in groups.items():
        lines.append(f"### {tag}")
        lines.append("")
        lines.append("| Método | Ruta | Permiso |")
        lines.append("|---|---|---|")
        lines.extend(rows)
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def replace_region(document: str, region: str) -> str:
    start = document.index(START)
    end = document.index(END) + len(END)
    return document[:start] + region + document[end:]


def main() -> int:
    if not DOC.exists():
        print(f"{DOC} does not exist yet; write the prose first.", file=sys.stderr)
        return 1
    document = DOC.read_text(encoding="utf-8")
    DOC.write_text(replace_region(document, render()), encoding="utf-8", newline="\n")
    print(f"Route table refreshed in {DOC}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
