"""Create the Postgres pool and the Redis client. Used by REST, gRPC and the worker."""

import asyncpg
from redis.asyncio import Redis

from app.config import Settings


async def create_pool(
    settings: Settings,
    min_size: int | None = None,
    max_size: int | None = None,
    command_timeout: float | None = None,
    url: str | None = None,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        url or settings.database_url,
        min_size=min_size or settings.db_pool_min,
        max_size=max_size or settings.db_pool_max,
        # Any SQL command slower than this raises TimeoutError.
        command_timeout=command_timeout or settings.db_command_timeout_seconds,
    )


async def create_read_pool(settings: Settings, primary: asyncpg.Pool) -> asyncpg.Pool:
    """Pool for the read replica, or the primary pool itself if there is no replica."""
    if not settings.database_read_url:
        return primary
    return await create_pool(settings, url=settings.database_read_url)


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        # Any Redis command slower than this raises redis.exceptions.TimeoutError.
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
    )
