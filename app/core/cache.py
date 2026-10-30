"""Redis helpers for the live counter hash post:{id}."""

import asyncpg
from redis.asyncio import Redis

DIRTY_POSTS = "dirty_posts"


def post_key(post_id: int) -> str:
    return f"post:{post_id}"


async def load_from_db(redis: Redis, pool: asyncpg.Pool, post_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Read counts from post_counters and put them into Redis with HSETNX.

    HSETNX never overwrites a value that is already in Redis, so a newer
    live count is safe. Posts that are not in Postgres start from 0.
    """
    rows = await pool.fetch(
        "SELECT post_id, likes, views FROM post_counters WHERE post_id = ANY($1::bigint[])",
        post_ids,
    )
    counts = {pid: (0, 0) for pid in post_ids}
    for row in rows:
        counts[row["post_id"]] = (row["likes"], row["views"])

    pipe = redis.pipeline()
    for pid, (likes, views) in counts.items():
        pipe.hsetnx(post_key(pid), "likes", likes)
        pipe.hsetnx(post_key(pid), "views", views)
    await pipe.execute()
    return counts


async def ensure_loaded(redis: Redis, pool: asyncpg.Pool, post_id: int) -> None:
    """Call before HINCRBY, so a cold key does not start from 0."""
    if not await redis.exists(post_key(post_id)):
        await load_from_db(redis, pool, [post_id])


async def incr(redis: Redis, field: str, post_id: int, delta: int) -> None:
    """HINCRBY the counter and mark the post as dirty, in one pipeline."""
    pipe = redis.pipeline()
    pipe.hincrby(post_key(post_id), field, delta)
    pipe.sadd(DIRTY_POSTS, post_id)
    await pipe.execute()
