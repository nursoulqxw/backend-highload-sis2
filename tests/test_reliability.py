"""Idempotency, timeouts and retries. Most tests run twice: with REST and with gRPC."""

import asyncio
import time

import grpc
import httpx
import pytest

from app.config import get_settings
from app.core.faults import FaultInjector
from app.grpc_api.generated import counter_pb2
from clients.errors import DeadlineExceededError, ServiceUnavailableError


async def counts(api, post_id: int) -> dict:
    return (await api.client.get_counters(user_id=10, post_ids=[post_id]))[0]


# ---------- Idempotency: calling twice = calling once ----------


async def test_like_twice_gives_one_like(api):
    await api.client.like_post(user_id=10, post_id=1)
    await api.client.like_post(user_id=10, post_id=1)
    assert (await counts(api, 1))["likes"] == 1


async def test_unlike_twice_gives_zero_likes(api):
    await api.client.like_post(user_id=10, post_id=1)
    await api.client.unlike_post(user_id=10, post_id=1)
    await api.client.unlike_post(user_id=10, post_id=1)
    assert (await counts(api, 1))["likes"] == 0


async def test_view_twice_in_one_hour_gives_one_view(api):
    await api.client.record_view(user_id=10, post_id=1)
    await api.client.record_view(user_id=10, post_id=1)
    assert (await counts(api, 1))["views"] == 1


# ---------- Retries ----------


async def test_retry_succeeds_after_one_failure(api):
    api.faults.fail_first_n = 1  # the server fails the first call with 503 / UNAVAILABLE

    await api.client.like_post(user_id=10, post_id=1)  # no error: the retry worked

    assert api.faults.failed_calls == 1
    assert (await counts(api, 1))["likes"] == 1


async def test_retry_stops_after_three_attempts(api):
    api.faults.fail_first_n = 100  # the server always fails

    with pytest.raises(ServiceUnavailableError):
        await api.client.like_post(user_id=10, post_id=1)

    assert api.faults.failed_calls == 3  # the first call + 2 retries, then we give up


@pytest.mark.parametrize("operation, field", [("like_post", "likes"), ("record_view", "views")])
async def test_retry_after_lost_response_does_not_count_twice(api, operation, field):
    # The server does the work, but the answer is "lost" (the server returns 503 / UNAVAILABLE).
    # The client retries the same call. Because the call is idempotent, we still count only 1.
    api.faults.fail_first_n = 1
    api.faults.fail_after_work = True

    await getattr(api.client, operation)(user_id=10, post_id=1)

    assert api.faults.failed_calls == 1  # the work really ran twice
    assert (await counts(api, 1))[field] == 1  # ... but was counted once


async def test_bad_request_is_not_retried(api):
    api.faults.fail_first_n = 0
    with pytest.raises((httpx.HTTPStatusError, grpc.aio.AioRpcError)):
        await api.client.like_post(user_id=10, post_id=0)  # 422 / INVALID_ARGUMENT: retry would not help


# ---------- Client timeouts (deadlines) ----------


async def test_slow_server_gives_deadline_exceeded_fast(api):
    api.faults.delay_ms = 2000  # the server needs 2 s

    started = time.monotonic()
    with pytest.raises(DeadlineExceededError):
        await api.client.like_post(user_id=10, post_id=1)  # deadline 0.5 s
    elapsed = time.monotonic() - started

    assert elapsed < 0.8  # we stopped waiting after ~0.5 s, we did not hang for 2 s


# ---------- Server timeouts: slow Postgres / Redis ----------


async def lock_likes_table(pool):
    """Hold a lock on the likes table, so every INSERT into it waits (a "slow Postgres")."""
    conn = await pool.acquire()
    transaction = conn.transaction()
    await transaction.start()
    await conn.execute("LOCK TABLE likes IN ACCESS EXCLUSIVE MODE")
    return conn, transaction


async def test_rest_returns_503_when_postgres_is_slow(rest, pool):
    conn, transaction = await lock_likes_table(pool)
    try:
        started = time.monotonic()
        response = await rest.put("/api/v1/posts/1/like", headers={"X-User-Id": "10"})
        elapsed = time.monotonic() - started
    finally:
        await transaction.rollback()
        await pool.release(conn)

    assert response.status_code == 503
    assert elapsed < 0.5  # the server gave up after its 200 ms timeout


async def test_grpc_returns_unavailable_when_postgres_is_slow(grpc_stub, pool):
    conn, transaction = await lock_likes_table(pool)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=10, post_id=1), timeout=2)
    finally:
        await transaction.rollback()
        await pool.release(conn)

    assert error.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_rest_returns_503_when_redis_is_slow(rest, redis):
    await redis.execute_command("CLIENT", "PAUSE", 400, "ALL")  # Redis answers nobody for 400 ms
    response = await rest.post("/api/v1/posts/1/view", headers={"X-User-Id": "10"})
    await asyncio.sleep(0.4)

    assert response.status_code == 503


async def test_grpc_returns_unavailable_when_redis_is_slow(grpc_stub, redis):
    await redis.execute_command("CLIENT", "PAUSE", 400, "ALL")
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=10, post_id=1), timeout=2)
    await asyncio.sleep(0.4)

    assert error.value.code() == grpc.StatusCode.UNAVAILABLE


# ---------- Fault injection settings ----------


def test_fault_injection_is_off_by_default():
    faults = FaultInjector.from_settings(get_settings())
    assert (faults.fail_first_n, faults.fail_after_work, faults.delay_ms) == (0, False, 0)


def test_fault_injection_reads_env_vars(monkeypatch):
    monkeypatch.setenv("FAULT_FAIL_FIRST_N", "2")
    monkeypatch.setenv("FAULT_DELAY_MS", "300")
    faults = FaultInjector.from_settings(get_settings())
    assert (faults.fail_first_n, faults.delay_ms) == (2, 300)
