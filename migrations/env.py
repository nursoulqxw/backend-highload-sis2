import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

# We write raw SQL in migrations, so there is no ORM metadata.
target_metadata = None


def get_url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_migrations_online())
