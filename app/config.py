from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All settings come from env vars (for example DATABASE_URL)."""

    database_url: str = "postgresql://postgres:postgres@localhost:5432/counters"
    # Optional read replica. GetCounters reads from it; all writes go to database_url.
    # Empty = read from database_url too.
    database_read_url: str = ""
    # True when DATABASE_URL points to PgBouncer (transaction mode). See resources.py.
    db_behind_pgbouncer: bool = False
    redis_url: str = "redis://localhost:6379/0"
    db_pool_min: int = 2
    db_pool_max: int = 10  # per process! N processes x 10 must stay below Postgres max_connections

    # Server-side timeouts: if Postgres or Redis is slower than this, we give up
    # and answer 503 (REST) / UNAVAILABLE (gRPC). We never hang.
    db_command_timeout_seconds: float = 0.2
    redis_timeout_seconds: float = 0.2

    # gRPC server
    grpc_port: int = 50051
    grpc_metrics_port: int = 9100
    # How many server processes. Python runs one thread of code at a time (GIL),
    # so one process uses at most one CPU core. N processes share one port (SO_REUSEPORT).
    grpc_workers: int = 1

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
