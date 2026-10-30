"""REST API: like, unlike, view, get counters, sync worker."""

from app.worker.sync import sync_once


def user(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


async def get_one(rest, post_id: int, user_id: int | None = None) -> dict:
    headers = user(user_id) if user_id else {}
    resp = await rest.get("/api/v1/counters", params={"post_ids": str(post_id)}, headers=headers)
    assert resp.status_code == 200
    return resp.json()[0]


async def test_like(rest):
    resp = await rest.put("/api/v1/posts/1/like", headers=user(10))
    assert resp.status_code == 204
    assert (await get_one(rest, 1))["likes"] == 1


async def test_unlike(rest):
    await rest.put("/api/v1/posts/1/like", headers=user(10))
    resp = await rest.delete("/api/v1/posts/1/like", headers=user(10))
    assert resp.status_code == 204
    assert (await get_one(rest, 1))["likes"] == 0


async def test_unlike_when_not_liked_changes_nothing(rest):
    await rest.put("/api/v1/posts/1/like", headers=user(10))
    await rest.delete("/api/v1/posts/1/like", headers=user(20))
    assert (await get_one(rest, 1))["likes"] == 1


async def test_view(rest):
    resp = await rest.post("/api/v1/posts/1/view", headers=user(10))
    assert resp.status_code == 204
    await rest.post("/api/v1/posts/1/view", headers=user(20))
    assert (await get_one(rest, 1))["views"] == 2


async def test_get_counters(rest):
    await rest.put("/api/v1/posts/1/like", headers=user(10))
    await rest.put("/api/v1/posts/1/like", headers=user(20))
    await rest.put("/api/v1/posts/2/like", headers=user(20))
    await rest.post("/api/v1/posts/1/view", headers=user(10))
    await rest.post("/api/v1/posts/2/view", headers=user(10))
    await rest.post("/api/v1/posts/2/view", headers=user(20))

    resp = await rest.get("/api/v1/counters", params={"post_ids": "1,2,3"}, headers=user(10))
    assert resp.status_code == 200
    assert resp.json() == [
        {"post_id": 1, "likes": 2, "views": 1, "liked_by_me": True},
        {"post_id": 2, "likes": 1, "views": 2, "liked_by_me": False},
        {"post_id": 3, "likes": 0, "views": 0, "liked_by_me": False},
    ]


async def test_cache_miss_reads_postgres(rest, pool, redis):
    # Postgres has old counts, Redis lost the key (for example after a restart).
    await pool.execute("INSERT INTO post_counters (post_id, likes, views) VALUES (7, 3, 42)")
    assert (await get_one(rest, 7)) == {"post_id": 7, "likes": 3, "views": 42, "liked_by_me": False}
    assert await redis.hgetall("post:7") == {"likes": "3", "views": "42"}


async def test_cold_key_is_loaded_before_increment(rest, pool):
    await pool.execute("INSERT INTO post_counters (post_id, likes, views) VALUES (5, 7, 100)")
    await rest.put("/api/v1/posts/5/like", headers=user(10))
    assert (await get_one(rest, 5))["likes"] == 8


async def test_missing_user_header_is_rejected(rest):
    assert (await rest.put("/api/v1/posts/1/like")).status_code == 422


async def test_health(rest):
    resp = await rest.get("/health")
    assert resp.status_code == 200


async def test_sync_worker_copies_counts_to_postgres(rest, pool, redis):
    await rest.put("/api/v1/posts/1/like", headers=user(10))
    await rest.post("/api/v1/posts/1/view", headers=user(10))
    await rest.post("/api/v1/posts/2/view", headers=user(10))

    assert await sync_once(pool, redis) == 2
    rows = await pool.fetch("SELECT post_id, likes, views FROM post_counters ORDER BY post_id")
    assert [tuple(r) for r in rows] == [(1, 1, 1), (2, 0, 1)]
    assert await redis.scard("dirty_posts") == 0
