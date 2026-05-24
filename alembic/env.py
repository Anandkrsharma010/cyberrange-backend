# alembic/env.py
"""
Alembic environment for CyberRange.

Configured for async SQLAlchemy (asyncpg driver).
Database URL is pulled from pydantic-settings config.

Uses MIGRATION_DATABASE_URL if set (dedicated DDL role),
falls back to DATABASE_URL if not.

Supports two modes:
  - Online (normal):  run_migrations_online() — used by `alembic upgrade`
  - Offline:          run_migrations_offline() — generates SQL without a live DB
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── Alembic config object — must come before set_main_option ─────────────────
config = context.config

# ── Load app config ───────────────────────────────────────────────────────────
from backend.config import get_settings

settings = get_settings()

# Use the dedicated migration role if configured, otherwise fall back to app URL
migration_url = settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL
# ConfigParser treats "%" as interpolation; double it so e.g. %40 in passwords works.
config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))

# Set up Python logging from the alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate (not used yet — set when we add ORM models).
target_metadata = None


# ── Offline mode ──────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations without a live database connection.
    Useful for generating a SQL script to review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode ───────────────────────────────────────────────────────────────

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations against a live database using an async engine.
    asyncpg requires this — the sync engine path won't work.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


# ── Entrypoint ────────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())