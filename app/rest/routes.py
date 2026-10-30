from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from redis.asyncio import Redis

from app.core.counters import get_counters
from app.core.faults import FaultInjector
from app.core.likes import like_post, unlike_post
from app.core.views import record_view
from app.rest.schemas import PostCounterOut

router = APIRouter(prefix="/api/v1")

MAX_POST_IDS = 100


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_faults(request: Request) -> FaultInjector:
    return request.app.state.faults


def parse_post_ids(post_ids: Annotated[str, Query(examples=["1,2,3"])]) -> list[int]:
    try:
        ids = [int(x) for x in post_ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(422, "post_ids must be a comma-separated list of integers") from exc
    if not ids or len(ids) > MAX_POST_IDS or any(i <= 0 for i in ids):
        raise HTTPException(422, f"post_ids must have 1..{MAX_POST_IDS} positive integers")
    return list(dict.fromkeys(ids))  # remove duplicates, keep order


# Reusable parameter types. FastAPI fills them in for every request.
PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
RedisDep = Annotated[Redis, Depends(get_redis)]
FaultsDep = Annotated[FaultInjector, Depends(get_faults)]
PostId = Annotated[int, Path(gt=0)]
UserId = Annotated[int, Header(alias="X-User-Id", gt=0)]
OptionalUserId = Annotated[int | None, Header(alias="X-User-Id", gt=0)]
PostIds = Annotated[list[int], Depends(parse_post_ids)]


@router.put("/posts/{post_id}/like", status_code=204)
async def like(
    post_id: PostId, user_id: UserId, pool: PoolDep, redis: RedisDep, faults: FaultsDep
) -> Response:
    async with faults.around_call():  # test-only faults, off by default
        await like_post(pool, redis, user_id, post_id)
    return Response(status_code=204)


@router.delete("/posts/{post_id}/like", status_code=204)
async def unlike(
    post_id: PostId, user_id: UserId, pool: PoolDep, redis: RedisDep, faults: FaultsDep
) -> Response:
    async with faults.around_call():  # test-only faults, off by default
        await unlike_post(pool, redis, user_id, post_id)
    return Response(status_code=204)


@router.post("/posts/{post_id}/view", status_code=204)
async def view(
    post_id: PostId, user_id: UserId, pool: PoolDep, redis: RedisDep, faults: FaultsDep
) -> Response:
    async with faults.around_call():  # test-only faults, off by default
        await record_view(pool, redis, user_id, post_id)
    return Response(status_code=204)


@router.get("/counters", response_model=list[PostCounterOut])
async def counters(
    post_ids: PostIds, pool: PoolDep, redis: RedisDep, faults: FaultsDep, user_id: OptionalUserId = None
):
    async with faults.around_call():
        return await get_counters(pool, redis, user_id, post_ids)
