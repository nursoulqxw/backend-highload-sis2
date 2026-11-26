import asyncpg
from redis.asyncio import Redis

from app.core import cache

SEEN_TTL_SECONDS = 3600


async def record_view(pool: asyncpg.Pool, redis: Redis, user_id: int, post_id: int) -> None:
    # One Lua script: SET seen:{user}:{post} NX EX 3600, then HINCRBY views + SADD dirty_posts.
    # If the same user already viewed this post in the last hour, nothing changes (idempotent).
    await cache.view(redis, pool, user_id, post_id, SEEN_TTL_SECONDS)


async def record_views(pool: asyncpg.Pool, redis: Redis, views: list[tuple[int, int]]) -> int:
    """Many (user_id, post_id) views at once. Returns how many were new (not duplicates)."""
    return await cache.view_batch(redis, pool, views, SEEN_TTL_SECONDS)
