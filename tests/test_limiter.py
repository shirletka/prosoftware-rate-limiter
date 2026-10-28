import asyncio
import os

import pytest

from app.limiter import SlidingWindowLimiter


@pytest.fixture
def limiter(redis, clock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(redis, limit=3, window_ms=10_000, clock=clock)


async def test_allows_requests_up_to_the_limit(limiter):
    remaining = [(await limiter.hit("key")).remaining for _ in range(3)]
    assert remaining == [2, 1, 0]


async def test_rejects_when_limit_is_reached(limiter, clock):
    for _ in range(3):
        await limiter.hit("key")
        clock.advance(seconds=1)

    decision = await limiter.hit("key")

    assert not decision.allowed
    assert decision.remaining == 0
    # The first request was made 3 s ago, it leaves the 10 s window in 7 s.
    assert decision.retry_after_ms == 7_000


async def test_window_boundary_is_exclusive_for_the_oldest_request(limiter, clock):
    for _ in range(3):
        await limiter.hit("key")

    clock.advance(ms=9_999)
    assert not (await limiter.hit("key")).allowed

    clock.advance(ms=1)
    assert (await limiter.hit("key")).allowed


async def test_window_slides_one_request_at_a_time(limiter, clock):
    start = clock.now_ms
    for offset in (0, 4_000, 8_000):
        clock.now_ms = start + offset
        await limiter.hit("key")

    clock.now_ms = start + 10_000
    assert (await limiter.hit("key")).allowed, "request from t=0 has left the window"

    clock.now_ms = start + 11_000
    decision = await limiter.hit("key")
    assert not decision.allowed, "requests from t=4, 8, 10 are still inside"
    assert decision.retry_after_ms == 3_000


async def test_rejected_requests_do_not_push_the_window(limiter, clock):
    for _ in range(3):
        await limiter.hit("key")
    for _ in range(20):
        clock.advance(seconds=0.4)
        await limiter.hit("key")

    clock.advance(seconds=2)  # 10 s after the first three
    assert (await limiter.hit("key")).allowed


async def test_keys_are_independent(limiter):
    for _ in range(3):
        await limiter.hit("alice")

    assert not (await limiter.hit("alice")).allowed
    assert (await limiter.hit("bob")).allowed


async def test_bucket_expires_with_the_window(limiter, redis):
    await limiter.hit("key")

    ttl = await redis.pttl(limiter.bucket("key"))
    assert 0 < ttl <= 10_000


async def test_raw_api_key_is_not_stored(limiter, redis):
    await limiter.hit("sk-secret-value")

    keys = [k.decode() for k in await redis.keys("*")]
    assert keys and not any("sk-secret-value" in k for k in keys)


async def test_lowered_limit_waits_for_enough_requests_to_expire(redis, clock):
    before = SlidingWindowLimiter(redis, limit=5, window_ms=10_000, clock=clock)
    for _ in range(5):
        await before.hit("key")
        clock.advance(seconds=1)

    # Redeployed with a lower limit: five requests in the window, only two allowed.
    after = SlidingWindowLimiter(redis, limit=2, window_ms=10_000, clock=clock)
    decision = await after.hit("key")

    assert not decision.allowed
    assert decision.remaining == 0
    # Requests made at t=0,1,2,3 must leave before the window holds fewer than two; t=3 leaves at 13 s, now is 5 s.
    assert decision.retry_after_ms == 8_000
    clock.advance(ms=decision.retry_after_ms - 1)
    assert not (await after.hit("key")).allowed
    clock.advance(ms=1)
    assert (await after.hit("key")).allowed


@pytest.mark.skipif(
    not os.environ.get("TEST_REDIS_URL"),
    reason="fakeredis runs commands one by one, so only real Redis can show a race; runs in docker compose",
)
async def test_concurrent_requests_never_exceed_the_limit(redis, clock):
    limiter = SlidingWindowLimiter(redis, limit=10, window_ms=60_000, clock=clock)

    decisions = await asyncio.gather(*(limiter.hit("key") for _ in range(100)))

    assert sum(d.allowed for d in decisions) == 10
