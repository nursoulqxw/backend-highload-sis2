"""gRPC server. It only translates Protobuf <-> app/core functions.

Run: python -m app.grpc_api
"""

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

import asyncpg
import grpc
from prometheus_client import start_http_server
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.core.counters import get_counters
from app.core.errors import BACKEND_ERRORS
from app.core.faults import FaultInjector
from app.core.likes import like_post, unlike_post
from app.core.metrics import track_request
from app.core.resources import create_pool, create_redis
from app.core.views import record_view
from app.grpc_api.generated import counter_pb2, counter_pb2_grpc

log = logging.getLogger("grpc_api")

MAX_POST_IDS = 100


class CounterService(counter_pb2_grpc.CounterServiceServicer):
    """Method names (LikePost, ...) must match proto/counter.proto exactly."""

    def __init__(self, pool: asyncpg.Pool, redis: Redis, faults: FaultInjector):
        self.pool = pool
        self.redis = redis
        self.faults = faults

    async def LikePost(self, request, context):
        await check_ids(context, request.user_id, request.post_id)
        async with self.backend_call(context, "LikePost"):
            await like_post(self.pool, self.redis, request.user_id, request.post_id)
        return counter_pb2.Empty()

    async def UnlikePost(self, request, context):
        await check_ids(context, request.user_id, request.post_id)
        async with self.backend_call(context, "UnlikePost"):
            await unlike_post(self.pool, self.redis, request.user_id, request.post_id)
        return counter_pb2.Empty()

    async def RecordView(self, request, context):
        await check_ids(context, request.user_id, request.post_id)
        async with self.backend_call(context, "RecordView"):
            await record_view(self.pool, self.redis, request.user_id, request.post_id)
        return counter_pb2.Empty()

    async def GetCounters(self, request, context):
        post_ids = list(dict.fromkeys(request.post_ids))  # remove duplicates, keep order
        if not post_ids or len(post_ids) > MAX_POST_IDS or any(i <= 0 for i in post_ids):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, f"post_ids must have 1..{MAX_POST_IDS} positive integers"
            )
        user_id = request.user_id if request.user_id > 0 else None
        async with self.backend_call(context, "GetCounters"):
            counters = await get_counters(self.pool, self.redis, user_id, post_ids)
        return counter_pb2.GetCountersResponse(
            counters=[
                counter_pb2.PostCounter(
                    post_id=c.post_id, likes=c.likes, views=c.views, liked_by_me=c.liked_by_me
                )
                for c in counters
            ]
        )

    @asynccontextmanager
    async def backend_call(self, context: grpc.aio.ServicerContext, operation: str):
        """Run the work. If Postgres / Redis is slow or down -> UNAVAILABLE (client may retry)."""
        with track_request("grpc", operation):
            try:
                async with self.faults.around_call():  # test-only faults, off by default
                    yield
            except BACKEND_ERRORS as exc:
                await context.abort(grpc.StatusCode.UNAVAILABLE, f"backend unavailable: {type(exc).__name__}")


async def check_ids(context: grpc.aio.ServicerContext, user_id: int, post_id: int) -> None:
    if user_id <= 0 or post_id <= 0:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "user_id and post_id must be positive")


async def start_server(settings: Settings, port: int) -> tuple[grpc.aio.Server, int, CounterService]:
    """Create pool + Redis, start the server. Port 0 = pick a free port (used in tests)."""
    pool = await create_pool(settings)
    redis = create_redis(settings)
    service = CounterService(pool, redis, FaultInjector.from_settings(settings))

    server = grpc.aio.server()
    counter_pb2_grpc.add_CounterServiceServicer_to_server(service, server)
    bound_port = server.add_insecure_port(f"[::]:{port}")
    await server.start()
    return server, bound_port, service


async def stop_server(server: grpc.aio.Server, service: CounterService, grace: float = 5.0) -> None:
    await server.stop(grace)  # finish running calls, refuse new ones
    await service.redis.aclose()
    await service.pool.close()


async def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    server, port, service = await start_server(settings, settings.grpc_port)
    log.info("gRPC server listening on port %d", port)
    # gRPC is not HTTP/1.1, so Prometheus reads the metrics from a small separate HTTP server.
    start_http_server(settings.grpc_metrics_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    await stop_server(server, service)
    log.info("stopped")
