import asyncio
from logging.config import fileConfig

from agent_fabric.config import get_settings
from agent_fabric.models import Base
from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def do_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = async_engine_from_config(config.get_section(config.config_ini_section) or {})
    async with engine.connect() as connection:
        await connection.run_sync(do_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
