from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.mixins import SYNC_SEQ_SEQUENCE_NAME
from src.modules.sync.models import SyncDevice, SyncOperation
from src.modules.sync.registry import FEED_ENTITIES, FeedEntity

#: A mapped row of a synchronizable table. The feed is deliberately generic — it
#: works off the registry, not off a closed union of model classes — so rows come
#: back untyped and their columns are read through the entity's field list.
SyncableRow = Any


class SyncRepository:
    """Persistence for devices, the operation log and the change feed."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_device(self, device_id: UUID) -> SyncDevice | None:
        device: SyncDevice | None = await self.session.scalar(
            select(SyncDevice).where(SyncDevice.id == device_id)
        )
        return device

    async def list_devices(self) -> list[SyncDevice]:
        statement = select(SyncDevice).order_by(SyncDevice.last_seen_at.desc().nullslast())
        return list((await self.session.scalars(statement)).all())

    async def get_operation(self, op_id: UUID) -> SyncOperation | None:
        operation: SyncOperation | None = await self.session.scalar(
            select(SyncOperation).where(SyncOperation.op_id == op_id)
        )
        return operation

    async def current_sync_seq(self) -> int:
        """Highest value the sequence has handed out so far.

        Read from `pg_sequences` rather than `nextval`, which would burn a value,
        and rather than `MAX(sync_seq)`, which would need a scan per table. On a
        sequence nobody has used yet `last_value` is NULL, hence the COALESCE.
        """
        value = await self.session.scalar(
            text("SELECT COALESCE(last_value, 0) FROM pg_sequences WHERE sequencename = :name"),
            {"name": SYNC_SEQ_SEQUENCE_NAME},
        )
        return int(value or 0)

    async def fetch_all_changes(
        self, cursor: int, limit: int
    ) -> list[tuple[FeedEntity, SyncableRow]]:
        """Merge every entity's changes into one stream ordered by `sync_seq`.

        Each table is queried separately and merged in memory: with a handful of
        tables and a 500-row page this is cheaper and far simpler than a UNION
        over heterogeneous columns.

        Each query takes `limit + 1` rows so the caller can tell "this page is
        full" from "there is more after it" even when a single table supplies the
        whole page.
        """
        collected: list[tuple[FeedEntity, SyncableRow]] = []
        for entity in FEED_ENTITIES:
            model = entity.model
            statement = (
                select(model)
                .where(model.sync_seq > cursor)
                .order_by(model.sync_seq)
                .limit(limit + 1)
            )
            collected.extend(
                (entity, row) for row in (await self.session.scalars(statement)).all()
            )

        collected.sort(key=lambda pair: pair[1].sync_seq)
        return collected

    def add(self, instance: object) -> None:
        self.session.add(instance)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
