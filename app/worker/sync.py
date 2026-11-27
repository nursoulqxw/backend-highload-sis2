"""Sync Worker: copies live counts from Redis to Postgres every few seconds."""

import asyncio
import contextlib
import logging
import signal

import asyncpg
from redis.asyncio import Redis

from app.config import get_settings
from app.core.cache import DIRTY_POSTS, post_key
from app.core.resources import create_pool, create_redis

log = logging.getLogger("sync_worker")

# ONE statement for the whole batch: the 3 arrays are turned into rows by unnest().
# Before (SIS #2) it was executemany = 1000 separate statements per batch.
# The WHERE skips rows that did not change: no new row version, no WAL.
UPSERT_SQL = """
INSERT INTO post_counters (post_id, likes, views, updated_at)
SELECT p, l, v, now() FROM unnest($1::bigint[], $2::bigint[], $3::bigint[]) AS t(p, l, v)
ON CONFLICT (post_id)
DO UPDATE SET likes = EXCLUDED.likes, views = EXCLUDED.views, updated_at = now()
WHERE (post_counters.likes, post_counters.views) IS DISTINCT FROM (EXCLUDED.likes, EXCLUDED.views)
"""


async def sync_once(pool: asyncpg.Pool, redis: Redis, batch_size: int = 1000) -> int:
    """Flush all dirty posts to Postgres. Returns how many posts were written."""
    total = 0
    while True:
        ids = await redis.spop(DIRTY_POSTS, batch_size)
        if not ids:
            return total

        pipe = redis.pipeline()
        for pid in ids:
            pipe.hmget(post_key(int(pid)), "likes", "views")
        results = await pipe.execute()

        rows = sorted(  # sorted by post_id: two writers lock rows in the same order -> no deadlock
            (int(pid), int(likes), int(views))
            for pid, (likes, views) in zip(ids, results, strict=True)
            if likes is not None and views is not None
        )
        if rows:
            post_ids, likes, views = (list(col) for col in zip(*rows, strict=True))
            try:
                async with pool.acquire() as conn, conn.transaction():
                    # post_counters is a COPY (the live values are in Redis). If Postgres crashes,
                    # we may lose the last ~0.6 s of this copy; Redis still has the values.
                    # So we do not wait for the WAL flush to disk: commits are much cheaper.
                    await conn.execute("SET LOCAL synchronous_commit = off")
                    await conn.execute(UPSERT_SQL, post_ids, likes, views)
            except BaseException:
                # SPOP already took these ids out of dirty_posts -> put them back, the next round retries.
                await redis.sadd(DIRTY_POSTS, *post_ids)
                raise
        total += len(rows)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    # The batch upsert of 1000 rows may need more than the 200 ms API timeout.
    pool = await create_pool(
        settings, min_size=1, max_size=2, command_timeout=settings.sync_command_timeout_seconds
    )
    redis = create_redis(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("started, interval=%ss", settings.sync_interval_seconds)
    try:
        while not stop.is_set():
            try:
                count = await sync_once(pool, redis, settings.sync_batch_size)
                if count:
                    log.info("synced %d posts", count)
            except Exception:
                log.exception("sync failed, will retry")
            # Sleep until the next round, but wake up at once on SIGTERM.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.sync_interval_seconds)
    finally:
        # Last flush, so nothing is left behind on shutdown.
        try:
            await sync_once(pool, redis, settings.sync_batch_size)
        except Exception:
            log.exception("final sync failed")
        await redis.aclose()
        await pool.close()
        log.info("stopped")
