"""Create the Postgres pool and the Redis client. Used by REST, gRPC and the worker."""

import asyncpg
from redis.asyncio import Redis

from app.config import Settings


async def _no_reset(conn: asyncpg.Connection) -> None:
    """Do not reset a connection when it goes back to the pool.

    By default asyncpg sends `RESET ALL; CLOSE ALL; UNLISTEN *; ...` on EVERY release.
    Behind PgBouncer (transaction mode) that is one more transaction per query: it needs its
    own PgBouncer slot, so every request takes 2 slots instead of 1. Under load the slots run
    out, the reset waits > 200 ms, times out, the connection is closed and opened again ->
    an avalanche of errors (we measured 88-100% failed calls).
    Safe for us: we use no session state (no LISTEN, advisory locks, cursors or SET; only
    SET LOCAL, which ends with the transaction anyway).
    """


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
        reset=_no_reset if settings.db_behind_pgbouncer else None,
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
