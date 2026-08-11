from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.core.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Fetch server-generated values (``server_default``, ``onupdate``) in the
    #: same statement, with ``RETURNING``, instead of leaving the attribute
    #: expired for a later SELECT.
    #:
    #: Under an async session that later SELECT is not a small cost but a crash:
    #: it happens outside the greenlet SQLAlchemy needs, and raises
    #: ``MissingGreenlet``. Every model here has an ``updated_at`` that the
    #: database fills on write and a response schema that reads it right after
    #: committing, so this belongs on the base and not on each model that
    #: remembers to ask.
    #:
    #: The ``noqa`` settles a disagreement between the two checkers: RUF012
    #: wants a ``ClassVar`` here, and ``ClassVar`` is exactly what mypy rejects,
    #: because ``DeclarativeBase`` already declares ``__mapper_args__`` as an
    #: instance attribute. SQLAlchemy owns this name and only ever reads it, so
    #: the shared-mutable-default hazard the rule is about does not apply.
    __mapper_args__: dict[str, Any] = {"eager_defaults": True}  # noqa: RUF012


settings = get_settings()
engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session
