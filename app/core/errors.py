"""Errors that mean "a backend (Postgres / Redis) is slow or down right now".

REST turns them into 503, gRPC into UNAVAILABLE. Both are "try again later",
so the client may retry.
"""

import asyncpg
import redis.exceptions


class ServiceUnavailableError(Exception):
    """Raised by fault injection (see app/core/faults.py)."""


BACKEND_ERRORS = (
    OSError,  # includes TimeoutError (asyncpg command_timeout) and refused connections
    redis.exceptions.RedisError,  # includes Redis TimeoutError and ConnectionError
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    ServiceUnavailableError,
)
