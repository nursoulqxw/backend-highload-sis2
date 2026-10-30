"""The same errors for both clients, so the caller does not care about the protocol."""


class DeadlineExceededError(Exception):
    """The whole call (all attempts together) took longer than the deadline."""


class ServiceUnavailableError(Exception):
    """The server said "try again later" (503 / UNAVAILABLE) and all attempts are used."""
