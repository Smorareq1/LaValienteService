"""Where the photographs of tickets live (Plan 0003 D9).

Its own storage and not `core/media` on purpose, for the two reasons D2 and D9
give. **D2:** everything this module needs is inside this module, so the retirement
is deleting a directory. **D9:** these files are not product photos — they carry a
customer's name, phone and NIT in their own handwriting, so they get their own
root (`SCAN_STORAGE_PATH`), their own size limit and a retention window, and they
are never served from a public path.

The format sniffing is shared with `core/media`: deciding what counts as an image
by its magic bytes is one rule and it should have one implementation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from anyio import to_thread

from src.core.config import get_settings
from src.core.exceptions import ConflictError
from src.core.media import detect_image_format


def scan_root() -> Path:
    return get_settings().scan_storage_path


def resolve(relative_path: str) -> Path:
    """Turn a stored path into a file on disk, refusing to leave the scan root.

    Same guard as `core/media.resolve`, and for the same reason: the paths this
    module writes are built from a UUID and can never escape, but the column is
    data like any other, and a row edited by hand must not make the API serve
    something outside the directory.
    """
    root = scan_root().resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise ConflictError("That scan path is outside the scan directory.")
    return candidate


async def store(scan_id: UUID, content: bytes) -> str:
    """Write the photograph and return the path to store on the row.

    Filed under the day it was taken (`2026/08/08/…`) because that is how the
    retention script of D9 deletes: a window of days is a directory range, not a
    scan of every file's timestamp.
    """
    settings = get_settings()
    if not content:
        raise ConflictError("The photo is empty.")
    if len(content) > settings.scan_max_image_bytes:
        raise ConflictError(
            f"The photo is larger than the {settings.scan_max_image_mb} MB limit."
        )

    detected = detect_image_format(content)
    if detected is None:
        raise ConflictError("That file is not a JPEG, PNG or WebP image.")

    day = datetime.now(UTC).strftime("%Y/%m/%d")
    digest = hashlib.sha256(content).hexdigest()[:12]
    relative = f"{day}/{scan_id}-{digest}.{detected}"
    destination = resolve(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Off the event loop: a few megabytes is a short write, but it is a blocking
    # one, and the counter is taking orders on the same process.
    await to_thread.run_sync(destination.write_bytes, content)
    return relative


async def read(relative_path: str) -> bytes | None:
    """The stored bytes, or `None` if the retention script already took them."""
    path = resolve(relative_path)
    if not path.is_file():
        return None
    return await to_thread.run_sync(path.read_bytes)


async def delete(relative_path: str | None) -> None:
    """Remove a stored photo. One already gone is not an error."""
    if not relative_path:
        return
    path = resolve(relative_path)
    await to_thread.run_sync(lambda: path.unlink(missing_ok=True))
