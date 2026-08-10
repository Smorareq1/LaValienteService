"""Delete scan photographs past their retention window (Plan 0003 D9).

The photos carry a customer's name, phone and NIT in their own handwriting. The
row stays — it is the record of what the model read and what had to be corrected,
which is the quality metric of §9 — but the image goes.

    python -m scripts.prune_scans [--dry-run]

Meant for a daily cron. Idempotent: a photo already gone is not an error.
"""

from __future__ import annotations

import argparse
import asyncio

from src.core.config import get_settings
from src.core.database import AsyncSessionFactory
from src.modules.intake_scan import storage
from src.modules.intake_scan.repository import ScanRepository


async def prune(*, dry_run: bool) -> None:
    settings = get_settings()
    days = settings.scan_image_retention_days

    async with AsyncSessionFactory() as session:
        repository = ScanRepository(session)
        expired = await repository.list_expired(days)

        if not expired:
            print(f"No scan photos older than {days} days.")
            return

        for job in expired:
            if dry_run:
                print(f"would delete {job.image_path} (scan {job.id})")
                continue
            await storage.delete(job.image_path)
            # Emptied rather than nulled: the column is not nullable, and an
            # empty path is what `list_expired` skips on the next run.
            job.image_path = ""

        if dry_run:
            print(f"{len(expired)} photos would be deleted.")
            return

        await repository.commit()
        print(f"Deleted {len(expired)} scan photos older than {days} days.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would go, delete nothing."
    )
    arguments = parser.parse_args()
    asyncio.run(prune(dry_run=arguments.dry_run))


if __name__ == "__main__":
    main()
