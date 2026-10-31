from dataclasses import dataclass
"answers - how many likes and views do these posts have, and did I like them?"

# pid is post_id, likes is how many likes, views is how many views, liked_by_me is whether the user liked it or not
# hmget is a Redis command that gets the values of multiple fields in a hash. In this case, it gets the number of likes and views for each post.

import asyncpg
from redis.asyncio import Redis

from app.core.cache import load_from_db, post_key


@dataclass
class PostCounter:
    post_id: int
    likes: int
    views: int
    liked_by_me: bool


async def get_counters(
    pool: asyncpg.Pool, redis: Redis, user_id: int | None, post_ids: list[int]
) -> list[PostCounter]:
    # 1. Read all hashes from Redis in one pipeline.
    pipe = redis.pipeline()
    for pid in post_ids:
        pipe.hmget(post_key(pid), "likes", "views")
    results = await pipe.execute()

    counts: dict[int, tuple[int, int]] = {}
    misses: list[int] = []
    for pid, (likes, views) in zip(post_ids, results, strict=True): # strict=True gives an error if the two lists have different lengths
        if likes is None or views is None:
            misses.append(pid)
        else:
            counts[pid] = (int(likes), int(views))

    # 2. Cache-aside: on a miss, read Postgres and fill Redis.
    if misses: # if the list isn't empty,
        counts.update(await load_from_db(redis, pool, misses))

    # 3. liked_by_me with one query.
    liked: set[int] = set()
    if user_id:
        rows = await pool.fetch(
            "SELECT post_id FROM likes WHERE user_id = $1 AND post_id = ANY($2::bigint[])",
            user_id,
            post_ids,
        )
        liked = {row["post_id"] for row in rows}

    return [PostCounter(pid, counts[pid][0], counts[pid][1], pid in liked) for pid in post_ids]
