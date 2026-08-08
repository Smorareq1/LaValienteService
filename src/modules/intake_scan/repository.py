from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.intake_scan.models import ScanJob


class ScanRepository:
    """Persistence for `scan_jobs`. Nothing else in the system reads this table."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, scan_id: UUID) -> ScanJob | None:
        job: ScanJob | None = await self.session.scalar(
            select(ScanJob).where(ScanJob.id == scan_id)
        )
        return job

    async def count_since(self, since: datetime) -> int:
        """Scans started since `since` — the daily cap of §8 (cost control).

        Counted over all of them and not per user: the budget belongs to the
        shop, not to whoever happens to be at the counter.
        """
        total = await self.session.scalar(
            select(func.count()).select_from(ScanJob).where(ScanJob.created_at >= since)
        )
        return total or 0

    async def count_today(self) -> int:
        return await self.count_since(
            datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        )

    async def list_expired(self, retention_days: int) -> list[ScanJob]:
        """Scans whose photo is past its retention window (D9)."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        rows = await self.session.scalars(
            select(ScanJob).where(ScanJob.created_at < cutoff, ScanJob.image_path != "")
        )
        return list(rows)

    def add(self, job: ScanJob) -> None:
        self.session.add(job)

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()

    async def refresh(self, job: ScanJob) -> None:
        await self.session.refresh(job)
