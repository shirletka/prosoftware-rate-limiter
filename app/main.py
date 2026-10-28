import logging
import math
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .config import Settings
from .limiter import Clock, Decision, SlidingWindowLimiter, wall_clock_ms

log = logging.getLogger(__name__)


def _seconds(ms: int) -> str:
    return str(max(1, math.ceil(ms / 1000)))


def _rate_headers(decision: Decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": _seconds(decision.reset_after_ms),
    }


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    clock: Clock = wall_clock_ms,
) -> FastAPI:
    settings = settings or Settings()
    redis = redis or Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
    )
    limiter = SlidingWindowLimiter(
        redis,
        limit=settings.rate_limit_requests,
        window_ms=settings.window_ms,
        clock=clock,
    )
    allowed_keys = settings.allowed_keys

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await redis.aclose()

    app = FastAPI(title="Echo API with sliding-window rate limit", lifespan=lifespan)

    def api_key(authorization: Annotated[str | None, Header()] = None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Expected 'Authorization: Bearer <key>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if allowed_keys and token not in allowed_keys:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Unknown API key",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return token

    async def rate_limit(key: Annotated[str, Depends(api_key)]) -> Decision | None:
        try:
            decision = await limiter.hit(key)
        except RedisError:
            if settings.rate_limit_fail_open:
                log.warning("Redis is unavailable, letting the request through", exc_info=True)
                return None
            log.exception("Redis is unavailable")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Rate limiter is unavailable") from None

        if not decision.allowed:
            headers = _rate_headers(decision) | {"Retry-After": _seconds(decision.retry_after_ms)}
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers=headers)
        return decision

    @app.post("/v1/echo", response_class=Response)
    async def echo(request: Request, decision: Annotated[Decision | None, Depends(rate_limit)]) -> Response:
        # Byte-for-byte echo: JSON, text or binary come back with the same Content-Type.
        # It goes into headers rather than media_type, which would append "; charset=utf-8" to text/*.
        headers = _rate_headers(decision) if decision else {}
        headers["Content-Type"] = request.headers.get("content-type", "application/octet-stream")
        return Response(await request.body(), headers=headers)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        try:
            await redis.ping()
        except RedisError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is unavailable") from None
        return {"status": "ok"}

    return app
