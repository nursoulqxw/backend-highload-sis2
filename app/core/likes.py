import asyncpg
from redis.asyncio import Redis

from app.core.cache import ensure_loaded, incr


def _row_count(status: str) -> int:
    # asyncpg returns a status string like "INSERT 0 1" or "DELETE 1".
    return int(status.split()[-1])


async def like_post(pool: asyncpg.Pool, redis: Redis, user_id: int, post_id: int) -> None:
    status = await pool.execute(
        "INSERT INTO likes (user_id, post_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        user_id,
        post_id,
    )
    if _row_count(status) == 0:
        return  # already liked -> nothing changes (idempotent)
    await ensure_loaded(redis, pool, post_id)
    await incr(redis, "likes", post_id, 1)


async def unlike_post(pool: asyncpg.Pool, redis: Redis, user_id: int, post_id: int) -> None:
    status = await pool.execute(
        "DELETE FROM likes WHERE user_id = $1 AND post_id = $2",
        user_id,
        post_id,
    )
    if _row_count(status) == 0:
        return  # was not liked -> nothing changes (idempotent)
    await ensure_loaded(redis, pool, post_id)
    await incr(redis, "likes", post_id, -1)
