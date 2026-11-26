"""RecordViews: many views in one call. Each test runs twice: with REST and with gRPC."""

import grpc
import httpx
import pytest

from app.core.cache import post_key


async def counts(api, post_ids: list[int]) -> dict[int, dict]:
    rows = await api.client.get_counters(user_id=10, post_ids=post_ids)
    return {row["post_id"]: row for row in rows}


async def test_batch_counts_each_new_view(api):
    counted = await api.client.record_views([(1, 100), (2, 100), (1, 200)])

    assert counted == 3
    result = await counts(api, [100, 200])
    assert result[100]["views"] == 2
    assert result[200]["views"] == 1


async def test_same_batch_twice_counts_once(api):
    batch = [(1, 100), (2, 100), (3, 200)]
    assert await api.client.record_views(batch) == 3
    assert await api.client.record_views(batch) == 0  # all duplicates (idempotent)
    result = await counts(api, [100, 200])
    assert (result[100]["views"], result[200]["views"]) == (2, 1)


async def test_duplicate_inside_one_batch_counts_once(api):
    assert await api.client.record_views([(1, 100), (1, 100)]) == 1


async def test_batch_retry_after_lost_response_does_not_count_twice(api):
    api.faults.fail_first_n = 1
    api.faults.fail_after_work = True  # the work is done, but the answer is "lost"

    await api.client.record_views([(1, 100), (2, 100)])  # the retry gets counted = 0, no error

    assert api.faults.failed_calls == 1
    assert (await counts(api, [100]))[100]["views"] == 2


async def test_batch_loads_cold_posts_from_postgres(api, pool, redis):
    await pool.execute("INSERT INTO post_counters (post_id, likes, views) VALUES (100, 5, 40)")
    await redis.delete(post_key(100))

    await api.client.record_views([(1, 100), (1, 101)])

    result = await counts(api, [100, 101])
    assert result[100]["views"] == 41  # 40 from Postgres + 1, not 0 + 1
    assert result[101]["views"] == 1  # a post nobody touched starts from 0


async def test_empty_batch_is_rejected(api):
    with pytest.raises((httpx.HTTPStatusError, grpc.aio.AioRpcError)):
        await api.client.record_views([])
