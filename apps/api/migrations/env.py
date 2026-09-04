from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.models import Base

config = context.config
if config.config_file_name is not None:
    # Alembic's template omits disable_existing_loggers, which defaults to True
    # and silently switches off every logger created before this point. That is
    # harmless for the migration process but not for the integration tests,
    # which run `alembic upgrade head` in-process and then still expect
    # `daily_insights` to emit.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
assert settings.database_url is not None
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
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
