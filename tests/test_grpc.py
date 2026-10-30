"""gRPC API: same logic as REST, so it must give the same results."""

import grpc
import pytest

from app.grpc_api.generated import counter_pb2


async def get_one(stub, post_id: int, user_id: int = 0) -> counter_pb2.PostCounter:
    resp = await stub.GetCounters(counter_pb2.GetCountersRequest(user_id=user_id, post_ids=[post_id]))
    return resp.counters[0]


async def test_like(grpc_stub):
    await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=10, post_id=1))
    assert (await get_one(grpc_stub, 1)).likes == 1


async def test_unlike(grpc_stub):
    await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=10, post_id=1))
    await grpc_stub.UnlikePost(counter_pb2.LikeRequest(user_id=10, post_id=1))
    assert (await get_one(grpc_stub, 1)).likes == 0


async def test_view(grpc_stub):
    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=10, post_id=1))
    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=20, post_id=1))
    assert (await get_one(grpc_stub, 1)).views == 2


async def test_get_counters(grpc_stub):
    await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=10, post_id=1))
    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=20, post_id=2))

    resp = await grpc_stub.GetCounters(counter_pb2.GetCountersRequest(user_id=10, post_ids=[1, 2, 3]))
    assert [(c.post_id, c.likes, c.views, c.liked_by_me) for c in resp.counters] == [
        (1, 1, 0, True),
        (2, 0, 1, False),
        (3, 0, 0, False),
    ]


async def test_rest_and_grpc_return_the_same_data(rest, grpc_stub):
    # Writes through BOTH protocols go to the same Redis + Postgres.
    await rest.put("/api/v1/posts/1/like", headers={"X-User-Id": "10"})
    await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=20, post_id=1))
    await rest.post("/api/v1/posts/2/view", headers={"X-User-Id": "10"})
    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=20, post_id=2))
    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=30, post_id=3))

    params, headers = {"post_ids": "1,2,3,4"}, {"X-User-Id": "10"}
    rest_resp = await rest.get("/api/v1/counters", params=params, headers=headers)
    grpc_resp = await grpc_stub.GetCounters(counter_pb2.GetCountersRequest(user_id=10, post_ids=[1, 2, 3, 4]))

    grpc_as_dicts = [
        {"post_id": c.post_id, "likes": c.likes, "views": c.views, "liked_by_me": c.liked_by_me}
        for c in grpc_resp.counters
    ]
    assert rest_resp.json() == grpc_as_dicts
    assert grpc_as_dicts[0] == {"post_id": 1, "likes": 2, "views": 0, "liked_by_me": True}


@pytest.mark.parametrize("user_id, post_id", [(0, 1), (10, 0), (-1, 5)])
async def test_bad_ids_give_invalid_argument(grpc_stub, user_id, post_id):
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await grpc_stub.LikePost(counter_pb2.LikeRequest(user_id=user_id, post_id=post_id))
    assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_empty_post_ids_give_invalid_argument(grpc_stub):
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await grpc_stub.GetCounters(counter_pb2.GetCountersRequest(user_id=10, post_ids=[]))
    assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT
