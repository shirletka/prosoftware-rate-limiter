from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_requests: int = Field(default=10, gt=0)
    rate_limit_window_seconds: float = Field(default=60, gt=0)
    rate_limit_fail_open: bool = False
    # A limiter that waits 5 s (redis-py default) for a dead Redis is worse than no limiter.
    redis_timeout_seconds: float = Field(default=0.5, gt=0)
    api_keys: str = ""

    @property
    def window_ms(self) -> int:
        return round(self.rate_limit_window_seconds * 1000)

    @property
    def allowed_keys(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())
