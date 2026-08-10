"""Building blocks shared by every synchronizable entity (Plan 0004 §6.1).

Any table the app mirrors locally carries three columns:

* ``version`` — optimistic-concurrency counter (D6). Update operations arriving
  from a device declare the ``base_version`` they were built on; a mismatch is a
  conflict, never a silent merge.
* ``sync_seq`` — position in the global change feed (D7). Every write takes the
  next value of a single PostgreSQL sequence, so ``pull`` is nothing more than
  ``WHERE sync_seq > :cursor ORDER BY sync_seq``.
* ``deleted_at`` — tombstone (D8). Synchronizable rows are never deleted, because
  a device that is offline today has to learn tomorrow that the row is gone.

Stamping is centralized in a session hook so a new module cannot forget it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, event, func
from sqlalchemy import Sequence as SaSequence
from sqlalchemy.orm import Mapped, Session, mapped_column

SYNC_SEQ_SEQUENCE_NAME = "sync_seq_global"

#: The single sequence that orders every change in the system.
sync_seq_sequence = SaSequence(SYNC_SEQ_SEQUENCE_NAME, metadata=None)


class SyncableMixin:
    """Adds the change-feed columns to a model. See the module docstring."""

    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    sync_seq: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


def _next_sync_seq() -> Any:
    """SQL expression that draws the next value of the global sequence.

    Assigned as an expression rather than a fetched integer so the write costs a
    single statement. The trade-off: after a flush the attribute is stale in
    Python and reading it emits a lazy SELECT — which under an async session
    raises ``MissingGreenlet``. Nothing needs ``sync_seq`` in memory (the pull
    reads it straight from the database), so do not read it after writing.
    """
    return func.nextval(SYNC_SEQ_SEQUENCE_NAME)


@event.listens_for(Session, "before_flush")
def _stamp_syncable_entities(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Stamp every synchronizable row being written with its feed position.

    Inserts take a sequence value and keep ``version = 1``; updates take a new
    sequence value and bump the version, which is what makes a device's stale
    ``base_version`` detectable.
    """
    for instance in session.new:
        if isinstance(instance, SyncableMixin):
            instance.sync_seq = _next_sync_seq()

    for instance in session.dirty:
        if not isinstance(instance, SyncableMixin):
            continue
        if not session.is_modified(instance, include_collections=False):
            continue
        instance.sync_seq = _next_sync_seq()
        # The counter lives in Python because the service already resolved the
        # optimistic-concurrency check; the write volume here is a handful of
        # rows per minute, not a contended hot row.
        instance.version = (instance.version or 1) + 1
