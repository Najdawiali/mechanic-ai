"""Alembic environment. Migrations are plain SQL, run in one transaction each."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

config = context.config
if config.config_file_name and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    """The URL passed by the caller (tests), else DATABASE_URL from the settings."""
    url = config.attributes.get("database_url") or get_settings().database_url
    # SQLAlchemy needs the driver named; psycopg 3 is the one this project installs.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported: run migrations against a database.")
run_migrations_online()
