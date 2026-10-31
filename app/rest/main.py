"""REST/JSON baseline. Run: uvicorn app.rest.main:app --port 8000"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.core.errors import BACKEND_ERRORS
from app.core.faults import FaultInjector
from app.core.resources import create_pool, create_redis
from app.rest.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = await create_pool(settings)
    app.state.redis = create_redis(settings)
    app.state.faults = FaultInjector.from_settings(settings)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(title="T7 Like & View Counter (REST)", lifespan=lifespan)
app.include_router(router)


def service_unavailable_response(exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": f"backend unavailable: {type(exc).__name__}"}, status_code=503)


async def backend_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Postgres or Redis was too slow (timeout) or down, or an injected fault -> 503, the client may retry."""
    return service_unavailable_response(exc)


for error_class in BACKEND_ERRORS:
    app.add_exception_handler(error_class, backend_error_handler)


@app.get("/health")
async def health(request: Request):
    try:
        await request.app.state.pool.fetchval("SELECT 1")
        await request.app.state.redis.ping()
    except BACKEND_ERRORS as exc:
        return service_unavailable_response(exc)
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus reads this page every 5 s."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
