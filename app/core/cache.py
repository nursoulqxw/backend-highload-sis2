"""Redis helpers for the live counter hash post:{id}.

The hot path (a view or a like) runs as ONE Lua script inside Redis:
one network round trip instead of three (SET NX, EXISTS, HINCRBY + SADD).
Redis runs a script atomically, so nothing can happen between its steps.
"""

import asyncpg
from redis.asyncio import Redis

DIRTY_POSTS = "dirty_posts"

# Return codes of the scripts.
DUPLICATE = 0  # already seen -> nothing changed (idempotent)
DONE = 1  # counter changed
COLD = -1  # post:{id} is not in Redis -> load it from Postgres, then call INCR_SCRIPT

# KEYS[1] = post:{id}, KEYS[2] = dirty_posts
# ARGV[1] = field (likes / views), ARGV[2] = delta, ARGV[3] = post id
INCR_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
redis.call('HINCRBY', KEYS[1], ARGV[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[3])
return 1
"""

# KEYS[1] = seen:{user}:{post}, KEYS[2] = post:{id}, KEYS[3] = dirty_posts
# ARGV[1] = TTL seconds, ARGV[2] = post id
# The cold check goes FIRST: if we must load from Postgres, the seen-key is not set yet,
# so if Postgres fails, the client's retry still counts the view.
VIEW_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 0 then return -1 end
if not redis.call('SET', KEYS[1], 1, 'NX', 'EX', ARGV[1]) then return 0 end
redis.call('HINCRBY', KEYS[2], 'views', 1)
redis.call('SADD', KEYS[3], ARGV[2])
return 1
"""


# Many views in one script call. For view i (0-based):
#   KEYS[2*i+1] = seen:{user}:{post}, KEYS[2*i+2] = post:{id}, ARGV[i+2] = post id
# ARGV[1] = TTL. The last KEY is dirty_posts.
# Returns one code per view: 1 = counted, 0 = duplicate, -1 = cold (nothing done yet).
VIEW_BATCH_SCRIPT = """
local dirty = KEYS[#KEYS]
local out = {}
for i = 1, #ARGV - 1 do
  local seen, post = KEYS[2*i-1], KEYS[2*i]
  if redis.call('EXISTS', post) == 0 then
    out[i] = -1
  elseif redis.call('SET', seen, 1, 'NX', 'EX', ARGV[1]) then
    redis.call('HINCRBY', post, 'views', 1)
    redis.call('SADD', dirty, ARGV[i+1])
    out[i] = 1
  else
    out[i] = 0
  end
end
return out
"""


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

    pipe = redis.pipeline(transaction=False)
    for pid, (likes, views) in counts.items():
        pipe.hsetnx(post_key(pid), "likes", likes)
        pipe.hsetnx(post_key(pid), "views", views)
    await pipe.execute()
    return counts


async def incr(redis: Redis, pool: asyncpg.Pool, field: str, post_id: int, delta: int) -> None:
    """HINCRBY the counter and mark the post as dirty. Cold key -> load from Postgres first.

    Usually 1 Redis round trip. Only a cold key (first touch after a Redis restart,
    or a post nobody touched before) also reads Postgres.
    """
    keys = [post_key(post_id), DIRTY_POSTS]
    args = [field, delta, post_id]
    script = _script(redis, "incr")
    if await script(keys=keys, args=args) == COLD:
        await load_from_db(redis, pool, [post_id])
        await script(keys=keys, args=args)


async def view(redis: Redis, pool: asyncpg.Pool, user_id: int, post_id: int, ttl: int) -> int:
    """Dedupe + count a view in one round trip. Returns DUPLICATE or DONE."""
    keys = [f"seen:{user_id}:{post_id}", post_key(post_id), DIRTY_POSTS]
    script = _script(redis, "view")
    result = await script(keys=keys, args=[ttl, post_id])
    if result == COLD:
        await load_from_db(redis, pool, [post_id])
        result = await script(keys=keys, args=[ttl, post_id])
    return int(result)


async def view_batch(redis: Redis, pool: asyncpg.Pool, views: list[tuple[int, int]], ttl: int) -> int:
    """Count many (user_id, post_id) views. Usually 1 Redis round trip for the whole batch.

    Cold posts: one SQL query for all of them, then one more script call for those views.
    Returns how many views were new.
    """
    script = _script(redis, "view_batch")

    async def run(batch: list[tuple[int, int]]) -> list[int]:
        keys: list[str] = []
        for user_id, post_id in batch:
            keys += [f"seen:{user_id}:{post_id}", post_key(post_id)]
        keys.append(DIRTY_POSTS)
        return await script(keys=keys, args=[ttl, *(post_id for _, post_id in batch)])

    codes = await run(views)
    counted = sum(1 for c in codes if c == DONE)
    cold = [v for v, c in zip(views, codes, strict=True) if c == COLD]
    if cold:
        await load_from_db(redis, pool, list({post_id for _, post_id in cold}))
        counted += sum(1 for c in await run(cold) if c == DONE)
    return counted


# --- script objects -------------------------------------------------------------
# register_script() sends EVALSHA (only a 40-char hash, not the whole script). If Redis
# forgot the script (for example after a restart), redis-py loads it again by itself.


def _script(redis: Redis, name: str):
    """One Script object per Redis client, made on first use."""
    scripts = getattr(redis, "t7_scripts", None)
    if scripts is None:
        scripts = {
            "incr": redis.register_script(INCR_SCRIPT),
            "view": redis.register_script(VIEW_SCRIPT),
            "view_batch": redis.register_script(VIEW_BATCH_SCRIPT),
        }
        redis.t7_scripts = scripts
    return scripts[name]
