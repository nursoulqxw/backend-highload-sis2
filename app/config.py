from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings come from env vars (for example DATABASE_URL)."""

    database_url: str = "postgresql://postgres:postgres@localhost:5432/counters"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_min: int = 2
    db_pool_max: int = 20

    # Server-side timeouts: if Postgres or Redis is slower than this, we give up
    # and answer 503 (REST) / UNAVAILABLE (gRPC). We never hang.
    db_command_timeout_seconds: float = 0.2
    redis_timeout_seconds: float = 0.2

    # gRPC server
    grpc_port: int = 50051

    # Sync Worker
    sync_interval_seconds: float = 5.0
    sync_batch_size: int = 1000
    sync_command_timeout_seconds: float = 5.0

    # Fault injection, ONLY for tests and demos. Off by default.
    fault_fail_first_n: int = 0  # the first N calls fail with 503 / UNAVAILABLE
    fault_fail_after_work: bool = False  # fail AFTER the work is done ("lost response")
    fault_delay_ms: int = 0  # every call waits this long first ("slow server")


def get_settings() -> Settings:
    return Settings()
