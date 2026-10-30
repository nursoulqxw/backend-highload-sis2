"""gRPC client with a deadline and retries.

Retries are done by the gRPC library itself. We only describe them in the
"service config" below (the same rules as clients/rest_client.py).
The deadline (timeout=0.5 s) covers the whole call, all attempts together.
"""

import json

import grpc

from app.grpc_api.generated import counter_pb2, counter_pb2_grpc
from clients.errors import DeadlineExceededError, ServiceUnavailableError

DEFAULT_TIMEOUT_SECONDS = 0.5

# Only these methods are retried. All 4 are idempotent (see README).
IDEMPOTENT_METHODS = ["LikePost", "UnlikePost", "RecordView", "GetCounters"]

SERVICE_CONFIG = {
    "methodConfig": [
        {
            "name": [{"service": "counter.v1.CounterService", "method": m} for m in IDEMPOTENT_METHODS],
            "retryPolicy": {
                "maxAttempts": 3,  # the first call + 2 retries
                "initialBackoff": "0.05s",  # wait random(0, 50 ms), then random(0, 100 ms), ...
                "maxBackoff": "0.2s",  # ... but never more than random(0, 200 ms)
                "backoffMultiplier": 2,
                "retryableStatusCodes": ["UNAVAILABLE"],  # "try again later"
            },
        }
    ]
}


class GrpcCounterClient:
    def __init__(self, target: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout
        # One channel = one HTTP/2 connection, many calls at the same time on it.
        self.channel = grpc.aio.insecure_channel(
            target,
            options=[
                ("grpc.enable_retries", 1),
                ("grpc.service_config", json.dumps(SERVICE_CONFIG)),
            ],
        )
        self.stub = counter_pb2_grpc.CounterServiceStub(self.channel)

    async def close(self) -> None:
        await self.channel.close()

    async def __aenter__(self) -> "GrpcCounterClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def like_post(self, user_id: int, post_id: int) -> None:
        await self.call(self.stub.LikePost, counter_pb2.LikeRequest(user_id=user_id, post_id=post_id))

    async def unlike_post(self, user_id: int, post_id: int) -> None:
        await self.call(self.stub.UnlikePost, counter_pb2.LikeRequest(user_id=user_id, post_id=post_id))

    async def record_view(self, user_id: int, post_id: int) -> None:
        await self.call(self.stub.RecordView, counter_pb2.ViewRequest(user_id=user_id, post_id=post_id))

    async def get_counters(self, user_id: int, post_ids: list[int]) -> list[dict]:
        request = counter_pb2.GetCountersRequest(user_id=user_id, post_ids=post_ids)
        response = await self.call(self.stub.GetCounters, request)
        return [
            {"post_id": c.post_id, "likes": c.likes, "views": c.views, "liked_by_me": c.liked_by_me}
            for c in response.counters
        ]

    async def call(self, method, request):
        try:
            return await method(request, timeout=self.timeout)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise DeadlineExceededError(f"deadline of {self.timeout}s exceeded") from exc
            if exc.code() == grpc.StatusCode.UNAVAILABLE:
                raise ServiceUnavailableError(exc.details()) from exc
            raise
