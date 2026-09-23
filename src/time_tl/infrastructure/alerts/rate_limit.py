"""A one-token limiter that deliberately never accumulates catch-up bursts."""

from dataclasses import dataclass


@dataclass
class NoCatchUpRateLimiter:
    requests_per_minute: float = 5.0
    _next_allowed: float = 0.0

    def __post_init__(self) -> None:
        if not 0 < self.requests_per_minute <= 5:
            raise ValueError("requests_per_minute must be in (0, 5]")

    @property
    def interval_seconds(self) -> float:
        return 60.0 / self.requests_per_minute

    def delay(self, now_monotonic: float) -> float:
        return max(0.0, self._next_allowed - now_monotonic)

    def consume(self, now_monotonic: float) -> bool:
        if now_monotonic < self._next_allowed:
            return False
        self._next_allowed = now_monotonic + self.interval_seconds
        return True
