"""REST/JSON client with a deadline and retries (exponential backoff + full jitter).

The retry rules are the same as the gRPC retryPolicy in clients/grpc_client.py:
- at most MAX_ATTEMPTS attempts (the first call + 2 retries);
- retry only on "try again later" answers: 502, 503, 504 or a lost connection;
- before each retry wait random(0, backoff), backoff = 50 ms, 100 ms, 200 ms (max);
- never go past the deadline: every attempt only gets the time that is left.

All 4 operations are idempotent (see README), so all of them may be retried.
"""

import asyncio
import random
import time

import httpx

from clients.errors import DeadlineExceededError, ServiceUnavailableError

DEFAULT_TIMEOUT_SECONDS = 0.5
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 0.05
MAX_BACKOFF_SECONDS = 0.2
RETRYABLE_STATUS_CODES = {502, 503, 504}


def backoff_seconds(retry_number: int) -> float:
    """Exponential backoff with full jitter. retry_number starts at 1."""
    backoff = min(MAX_BACKOFF_SECONDS, INITIAL_BACKOFF_SECONDS * 2 ** (retry_number - 1))
    return random.uniform(0, backoff)


class RestCounterClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, max_connections: int = 100):
        self.timeout = timeout
        # One client = a pool of keep-alive HTTP/1.1 connections.
        self.http = httpx.AsyncClient(
            base_url=base_url,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "RestCounterClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def like_post(self, user_id: int, post_id: int) -> None:
        await self.send("PUT", f"/api/v1/posts/{post_id}/like", user_id)

    async def unlike_post(self, user_id: int, post_id: int) -> None:
        await self.send("DELETE", f"/api/v1/posts/{post_id}/like", user_id)

    async def record_view(self, user_id: int, post_id: int) -> None:
        await self.send("POST", f"/api/v1/posts/{post_id}/view", user_id)

    async def get_counters(self, user_id: int, post_ids: list[int]) -> list[dict]:
        params = {"post_ids": ",".join(map(str, post_ids))}
        response = await self.send("GET", "/api/v1/counters", user_id, params)
        return response.json()

    async def send(self, method: str, path: str, user_id: int, params: dict | None = None) -> httpx.Response:
        deadline = time.monotonic() + self.timeout
        headers = {"X-User-Id": str(user_id)}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                raise DeadlineExceededError(f"{method} {path}: deadline of {self.timeout}s exceeded")
            try:
                # asyncio.timeout limits the WHOLE attempt (httpx timeouts are per phase).
                async with asyncio.timeout(time_left):
                    response = await self.http.request(method, path, headers=headers, params=params)
            except (TimeoutError, httpx.TimeoutException) as exc:
                # This attempt used all the time that was left -> no time for a retry.
                raise DeadlineExceededError(f"{method} {path}: deadline of {self.timeout}s exceeded") from exc
            except httpx.TransportError as exc:
                last_error = ServiceUnavailableError(f"{method} {path}: {type(exc).__name__}")
            else:
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()  # 4xx is our bug, a retry will not help
                    return response
                last_error = ServiceUnavailableError(f"{method} {path}: HTTP {response.status_code}")

            if attempt == MAX_ATTEMPTS:
                break
            delay = backoff_seconds(attempt)
            if time.monotonic() + delay >= deadline:
                break  # waiting would go past the deadline
            await asyncio.sleep(delay)

        raise last_error
