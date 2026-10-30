import asyncpg
from redis.asyncio import Redis

from app.core.cache import ensure_loaded, incr

SEEN_TTL_SECONDS = 3600


async def record_view(pool: asyncpg.Pool, redis: Redis, user_id: int, post_id: int) -> None:
    first_view = await redis.set(f"seen:{user_id}:{post_id}", 1, nx=True, ex=SEEN_TTL_SECONDS)
    if not first_view:
        return  # same user already viewed this post in the last hour (idempotent)
    await ensure_loaded(redis, pool, post_id)
    await incr(redis, "views", post_id, 1)
