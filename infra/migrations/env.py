"""Alembic environment for the N1.2 PostgreSQL-only schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from owcopilot.teamdb.schema import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("OWCOPILOT_MIGRATION_DATABASE_URL")
if not database_url:
    raise RuntimeError("OWCOPILOT_MIGRATION_DATABASE_URL is required; no migration fallback exists")
migration_url = make_url(database_url)
if migration_url.drivername != "postgresql+psycopg":
    raise RuntimeError("migration URL must use the postgresql+psycopg driver")
if migration_url.username != "owcopilot_migration":
    raise RuntimeError("migration URL must authenticate as owcopilot_migration")
if migration_url.database in {None, ""}:
    raise RuntimeError("migration URL must name an explicit database")
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit PostgreSQL SQL without opening a connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations transactionally using a non-pooled migration connection."""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
