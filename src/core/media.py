"""Files that belong to a record but not in the database (Plan 0005 D10, §8.4).

Only the *path* is ever stored in a column. The bytes live under `MEDIA_DIR`,
which is a local directory today and could be object storage tomorrow without a
migration — and, just as important, they never travel in the sync feed: a phone
that mirrors the catalog does not need to mirror a photo it can fetch by HTTP.

The one caller today is the product image of `inventory`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from anyio import to_thread

from src.core.config import get_settings
from src.core.exceptions import ConflictError

#: Magic bytes, not the file name. Anyone can rename `payload.exe` to `.png`, and
#: what ends up in `MEDIA_DIR` is served back over HTTP later.
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RIFF_SIGNATURE = b"RIFF"
WEBP_TAG = b"WEBP"

#: `jpeg` and `jpg` are one format written two ways; the setting may list either.
FORMAT_ALIASES = {"jpeg": "jpg"}

PRODUCT_IMAGES = "products"


def detect_image_format(content: bytes) -> str | None:
    """The real format of `content`, or `None` if it is not an image we take."""
    if content.startswith(JPEG_SIGNATURE):
        return "jpg"
    if content.startswith(PNG_SIGNATURE):
        return "png"
    if content.startswith(RIFF_SIGNATURE) and content[8:12] == WEBP_TAG:
        return "webp"
    return None


def _allowed_formats() -> set[str]:
    settings = get_settings()
    return {
        FORMAT_ALIASES.get(fmt.strip().lower().lstrip("."), fmt.strip().lower().lstrip("."))
        for fmt in settings.media_allowed_formats
    }


def media_root() -> Path:
    return get_settings().media_dir


def resolve(relative_path: str) -> Path:
    """Turn a stored path into a file on disk, refusing to leave `MEDIA_DIR`.

    The paths this module writes are built from a UUID and can never escape, but
    the column is data like any other: a row edited by hand, or restored from an
    older dump, must not be able to make the API serve `/etc/passwd`.
    """
    root = media_root().resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise ConflictError("That media path is outside the media directory.")
    return candidate


async def store_product_image(product_id: UUID, content: bytes) -> str:
    """Write a product's image and return the path to store on the row.

    The name carries a short digest of the contents, so replacing an image
    changes `image_path`. Without that the app would keep showing the old photo
    from its cache until something else evicted it — the URL is the same one it
    fetched yesterday.
    """
    settings = get_settings()
    if not content:
        raise ConflictError("The image file is empty.")
    if len(content) > settings.media_max_image_bytes:
        raise ConflictError(
            f"The image is larger than the {settings.media_max_image_mb} MB limit."
        )

    detected = detect_image_format(content)
    if detected is None:
        raise ConflictError("That file is not a JPEG, PNG or WebP image.")
    if detected not in _allowed_formats():
        raise ConflictError(f"Images in {detected} are not accepted.")

    digest = hashlib.sha256(content).hexdigest()[:12]
    relative = f"{PRODUCT_IMAGES}/{product_id}-{digest}.{detected}"
    destination = resolve(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Off the event loop: five megabytes is a short write, but it is a *blocking*
    # one, and the counter is taking orders on the same process.
    await to_thread.run_sync(destination.write_bytes, content)
    return relative


async def delete(relative_path: str | None) -> None:
    """Remove a stored file. A path already gone is not an error.

    Called when an image is replaced. A file left behind would be invisible
    forever — nothing points at it — and `MEDIA_DIR` would grow one orphan per
    edit.
    """
    if not relative_path:
        return
    path = resolve(relative_path)
    await to_thread.run_sync(lambda: path.unlink(missing_ok=True))
