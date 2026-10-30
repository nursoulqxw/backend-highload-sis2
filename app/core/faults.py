"""Fault injection for tests and demos. Off by default (all settings are 0).

It lets us show that timeouts and retries really work:
- delay_ms > 0          -> the server is slow, so the client hits its deadline.
- fail_first_n > 0      -> the first N calls fail with 503 / UNAVAILABLE, so the client retries.
- fail_after_work=True  -> the call does the work, THEN fails. The client never sees
                           the answer and retries. Idempotency keeps the counters correct.
"""

import asyncio
from contextlib import asynccontextmanager

from app.config import Settings
from app.core.errors import ServiceUnavailableError


class FaultInjector:
    def __init__(self, fail_first_n: int = 0, fail_after_work: bool = False, delay_ms: int = 0):
        self.fail_first_n = fail_first_n
        self.fail_after_work = fail_after_work
        self.delay_ms = delay_ms
        self.failed_calls = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> "FaultInjector":
        return cls(settings.fault_fail_first_n, settings.fault_fail_after_work, settings.fault_delay_ms)

    @asynccontextmanager
    async def around_call(self):
        """Wrap the real work: `async with faults.around_call(): await like_post(...)`."""
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if not self.fail_after_work:
            self._maybe_fail()
        yield
        if self.fail_after_work:
            self._maybe_fail()

    def _maybe_fail(self) -> None:
        if self.failed_calls < self.fail_first_n:
            self.failed_calls += 1
            raise ServiceUnavailableError("injected fault")
