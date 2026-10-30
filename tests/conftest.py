import asyncio
from dataclasses import dataclass

import asyncpg
import grpc
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from httpx import AsyncClient
from redis.asyncio import Redis
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from app.config import get_settings
from app.core.faults import FaultInjector
from app.grpc_api.generated import counter_pb2_grpc
from app.grpc_api.server import start_server, stop_server
from clients.grpc_client import GrpcCounterClient
from clients.rest_client import RestCounterClient


@pytest.fixture(scope="session")
def containers():
    """Start real Postgres 16 and Redis 7 once for all tests, and run the migration."""
    with PostgresContainer("postgres:16-alpine", driver=None) as pg, RedisContainer("redis:7-alpine") as rd:
        db_url = pg.get_connection_url()
        redis_url = f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0"

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        yield db_url, redis_url


@pytest.fixture
def env(containers, monkeypatch):
    db_url, redis_url = containers
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("REDIS_URL", redis_url)


@pytest.fixture
async def pool(env, containers):
    """Our own Postgres connection for checks. Cleans the tables before each test."""
    pool = await asyncpg.create_pool(containers[0], min_size=1, max_size=3)
    await pool.execute("TRUNCATE likes, post_counters")
    yield pool
    await pool.close()


@pytest.fixture
async def redis(env, containers):
    """Our own Redis connection for checks. Cleans Redis before each test."""
    redis = Redis.from_url(containers[1], decode_responses=True)
    await redis.flushdb()
    yield redis
    await redis.aclose()


@pytest.fixture
async def rest_server(pool, redis):
    """Start the real REST server (Uvicorn) on a free port. Yields (app, base_url)."""
    from app.rest.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # startup failed -> show the error
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest.fixture
async def grpc_server(pool, redis):
    """Start the real gRPC server on a free port. Yields (service, target)."""
    server, port, service = await start_server(get_settings(), port=0)
    yield service, f"127.0.0.1:{port}"
    await stop_server(server, service, grace=0)


@pytest.fixture
async def rest(rest_server):
    """A plain HTTP client, no retries."""
    _, base_url = rest_server
    async with AsyncClient(base_url=base_url) as client:
        yield client


@pytest.fixture
async def grpc_stub(grpc_server):
    """A plain gRPC stub, no retries."""
    _, target = grpc_server
    async with grpc.aio.insecure_channel(target) as channel:
        yield counter_pb2_grpc.CounterServiceStub(channel)


@dataclass
class Api:
    """One protocol under test: its client (with deadline + retries) and the server's fault injector."""

    protocol: str
    client: RestCounterClient | GrpcCounterClient
    faults: FaultInjector


@pytest.fixture(params=["rest", "grpc"])
async def api(request, rest_server, grpc_server):
    """Runs the test twice: once with REST, once with gRPC."""
    if request.param == "rest":
        app, base_url = rest_server
        async with RestCounterClient(base_url) as client:
            yield Api("rest", client, app.state.faults)
    else:
        service, target = grpc_server
        async with GrpcCounterClient(target) as client:
            yield Api("grpc", client, service.faults)
