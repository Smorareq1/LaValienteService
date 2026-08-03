from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.core.config import get_settings
from src.core.database import Base

# The module imports below are for their side effect: every module has to
# register its tables on `Base.metadata` before autogenerate compares it against
# the database, or the comparison reads as "drop everything that is not
# identity".
from src.modules.catalog import models as catalog_models  # noqa: F401
from src.modules.customers import models as customers_models  # noqa: F401
from src.modules.identity import models as identity_models  # noqa: F401
from src.modules.orders import models as orders_models  # noqa: F401
from src.modules.sync import models as sync_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
