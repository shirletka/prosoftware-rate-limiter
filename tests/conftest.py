import os

import fakeredis
import pytest
from redis.asyncio import Redis


class FakeClock:
    def __init__(self, start_ms: int = 1_767_225_600_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, *, seconds: float = 0, ms: int = 0) -> None:
        self.now_ms += round(seconds * 1000) + ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def redis():
    # Real Redis in docker compose (TEST_REDIS_URL), fakeredis everywhere else.
    url = os.environ.get("TEST_REDIS_URL")
    client = Redis.from_url(url) if url else fakeredis.FakeAsyncRedis()
    await client.flushdb()
    yield client
    await client.aclose()
