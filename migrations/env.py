import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def database_url() -> str:
    direct = os.getenv("MIGRATIONS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if direct:
        if direct.startswith("postgresql://"):
            return "postgresql+psycopg://" + direct.removeprefix("postgresql://")
        return direct
    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("DB_MIGRATION_USER", os.getenv("DB_USER", "rss_migrator")),
        password=os.getenv("DB_MIGRATION_PASSWORD", os.getenv("DB_PASSWORD", "")),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "rss_agent"),
    ).render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url().replace("%", "%%")
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        pool_pre_ping=True,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
