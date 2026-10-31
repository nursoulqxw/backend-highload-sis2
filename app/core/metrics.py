"""Prometheus metrics. REST and gRPC record the same metrics in the same way."""

import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "counter_requests_total",
    "Number of finished requests.",
    ["protocol", "operation", "status"],
)
LATENCY = Histogram(
    "counter_request_duration_seconds",
    "Time the server spent on a request.",
    ["protocol", "operation"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)


@contextmanager
def track_request(protocol: str, operation: str):
    """Count one request and measure its time: `with track_request("rest", "LikePost"): ...`."""
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except BaseException:  # also catches CancelledError, when the client stops waiting
        status = "error"
        raise
    finally:
        REQUESTS.labels(protocol, operation, status).inc()
        LATENCY.labels(protocol, operation).observe(time.perf_counter() - started)
