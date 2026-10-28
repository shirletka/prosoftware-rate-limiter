import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis

Clock = Callable[[], int]

_SCRIPT = Path(__file__).with_name("sliding_window.lua").read_text()


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_ms: int
    """Time until one more slot frees up (usually: until the oldest request leaves the window)."""

    @property
    def retry_after_ms(self) -> int:
        return 0 if self.allowed else self.reset_after_ms


class SlidingWindowLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        limit: int,
        window_ms: int,
        clock: Clock = wall_clock_ms,
        prefix: str = "ratelimit:",
    ) -> None:
        if limit < 1 or window_ms < 1:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window_ms = window_ms
        self._clock = clock
        self._prefix = prefix
        self._script = redis.register_script(_SCRIPT)

    def bucket(self, api_key: str) -> str:
        # Raw keys never reach Redis: anyone with access to a dump would otherwise get working credentials.
        return self._prefix + hashlib.sha256(api_key.encode()).hexdigest()

    async def hit(self, api_key: str) -> Decision:
        now = self._clock()
        allowed, count, blocking = await self._script(
            keys=[self.bucket(api_key)],
            args=[now, self.window_ms, self.limit, uuid.uuid4().hex],
        )
        return Decision(
            allowed=bool(allowed),
            limit=self.limit,
            remaining=max(self.limit - int(count), 0),
            reset_after_ms=int(blocking) + self.window_ms - now,
        )
