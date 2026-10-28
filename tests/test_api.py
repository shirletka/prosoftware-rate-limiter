import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

ALICE = {"Authorization": "Bearer alice-key"}
BOB = {"Authorization": "Bearer bob-key"}


def make_client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(redis, clock):
    settings = Settings(rate_limit_requests=2, rate_limit_window_seconds=5)
    async with make_client(create_app(settings, redis=redis, clock=clock)) as c:
        yield c


async def test_echoes_the_body(client):
    response = await client.post("/v1/echo", json={"hello": "world", "n": [1, 2]}, headers=ALICE)

    assert response.status_code == 200
    assert response.json() == {"hello": "world", "n": [1, 2]}
    assert response.headers["content-type"] == "application/json"


async def test_echoes_non_json_bodies_byte_for_byte(client):
    body = "Сәлем, әлем".encode() + b"\xff\x00"

    response = await client.post("/v1/echo", content=body, headers=ALICE | {"Content-Type": "text/plain"})

    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "text/plain"


async def test_reports_remaining_quota(client):
    first = await client.post("/v1/echo", json={}, headers=ALICE)
    second = await client.post("/v1/echo", json={}, headers=ALICE)

    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.headers["X-RateLimit-Remaining"] == "0"


async def test_returns_429_with_retry_after_when_limit_exceeded(client, clock):
    await client.post("/v1/echo", json={}, headers=ALICE)
    clock.advance(seconds=1.2)
    await client.post("/v1/echo", json={}, headers=ALICE)

    response = await client.post("/v1/echo", json={}, headers=ALICE)

    assert response.status_code == 429
    # 3.8 s until the first request leaves the window, rounded up to whole seconds.
    assert response.headers["Retry-After"] == "4"
    assert response.headers["X-RateLimit-Remaining"] == "0"


async def test_accepts_requests_again_after_the_window_moves(client, clock):
    for _ in range(2):
        await client.post("/v1/echo", json={}, headers=ALICE)
    assert (await client.post("/v1/echo", json={}, headers=ALICE)).status_code == 429

    clock.advance(seconds=5)

    response = await client.post("/v1/echo", json={}, headers=ALICE)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == "1"


async def test_limits_are_per_key(client):
    for _ in range(2):
        await client.post("/v1/echo", json={}, headers=ALICE)

    assert (await client.post("/v1/echo", json={}, headers=ALICE)).status_code == 429
    assert (await client.post("/v1/echo", json={}, headers=BOB)).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}])
async def test_requires_bearer_token(client, headers):
    response = await client.post("/v1/echo", json={}, headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


async def test_rejects_keys_outside_the_allowlist(redis, clock):
    app = create_app(Settings(api_keys="alice-key, other"), redis=redis, clock=clock)
    async with make_client(app) as client:
        assert (await client.post("/v1/echo", json={}, headers=ALICE)).status_code == 200
        assert (await client.post("/v1/echo", json={}, headers=BOB)).status_code == 401


async def test_healthz_reports_redis_outage(clock):
    server = fakeredis.FakeServer()
    server.connected = False
    app = create_app(Settings(), redis=fakeredis.FakeAsyncRedis(server=server), clock=clock)
    async with make_client(app) as client:
        assert (await client.get("/healthz")).status_code == 503


@pytest.mark.parametrize(("fail_open", "expected"), [(False, 503), (True, 200)])
async def test_redis_outage(clock, fail_open, expected):
    server = fakeredis.FakeServer()
    server.connected = False
    app = create_app(
        Settings(rate_limit_fail_open=fail_open),
        redis=fakeredis.FakeAsyncRedis(server=server),
        clock=clock,
    )
    async with make_client(app) as client:
        response = await client.post("/v1/echo", json={}, headers=ALICE)

    assert response.status_code == expected
