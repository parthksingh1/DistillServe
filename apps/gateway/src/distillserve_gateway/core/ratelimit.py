"""Per-tenant token-bucket rate limiting.

A token bucket rather than a fixed window, because a fixed window lets a tenant
send its whole minute's quota in the last second of one window and again in the
first second of the next — a 2x burst against the upstream provider precisely
when you are trying to stay under *its* rate limit.

Two stores implement one protocol:

* **Redis**, via a Lua script so that read-decide-write is atomic across every
  gateway replica. Doing it in Python would race: two replicas both read 1
  token left and both allow.
* **In-memory**, for a single process with no Redis. It is correct for one
  replica and honestly wrong for several, which is why `/readyz` reports
  degraded when it is in use rather than pretending the limit is enforced.

The bucket refills continuously from a timestamp instead of on a timer, so
there is no background task and a tenant that has been idle for a minute has a
full bucket the instant it returns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Atomic refill-and-consume. Returns {allowed, remaining, retry_after_ms}.
#:
#: Written in Lua rather than a WATCH/MULTI loop because the retry loop's
#: failure mode under contention is exactly the case that matters: many
#: concurrent requests from one busy tenant.
_REFILL_AND_CONSUME_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])        -- tokens per second
local capacity = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  ts = now_ms
end

-- Continuous refill: elapsed time since the last call, converted to tokens.
local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry_after_ms = math.ceil(((cost - tokens) / rate) * 1000)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl)
return {allowed, math.floor(tokens), retry_after_ms}
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of one rate-limit check."""

    allowed: bool
    remaining: int
    retry_after_ms: int
    limit: int
    distributed: bool
    """False when the decision came from a process-local bucket."""

    @property
    def retry_after_seconds(self) -> int:
        """Retry-After header value, rounded up to whole seconds."""
        return max(1, -(-self.retry_after_ms // 1000))


@runtime_checkable
class RateLimiter(Protocol):
    """Anything that can admit or reject a tenant's request."""

    @property
    def distributed(self) -> bool:
        """Whether this limiter is shared across gateway replicas."""
        ...

    async def check(
        self, tenant_id: str, *, requests_per_minute: int, cost: int = 1
    ) -> RateLimitDecision:
        """Consume ``cost`` tokens from ``tenant_id``'s bucket."""
        ...


class InMemoryRateLimiter:
    """Process-local token bucket.

    Correct for a single replica; deliberately reports ``distributed = False``
    so readiness can say so rather than implying a guarantee it cannot make.
    """

    def __init__(self) -> None:
        """Start with no buckets; each is created full on first use."""
        self._buckets: dict[str, tuple[float, float]] = {}

    @property
    def distributed(self) -> bool:
        """Never shared across processes."""
        return False

    async def check(
        self, tenant_id: str, *, requests_per_minute: int, cost: int = 1
    ) -> RateLimitDecision:
        """Consume tokens from the tenant's bucket, refilling by elapsed time."""
        rate = requests_per_minute / 60.0
        capacity = float(requests_per_minute)
        now = time.monotonic()

        tokens, last = self._buckets.get(tenant_id, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - last) * rate)

        if tokens >= cost:
            self._buckets[tenant_id] = (tokens - cost, now)
            return RateLimitDecision(
                allowed=True,
                remaining=int(tokens - cost),
                retry_after_ms=0,
                limit=requests_per_minute,
                distributed=False,
            )

        self._buckets[tenant_id] = (tokens, now)
        return RateLimitDecision(
            allowed=False,
            remaining=int(tokens),
            retry_after_ms=int(((cost - tokens) / rate) * 1000),
            limit=requests_per_minute,
            distributed=False,
        )


class RedisRateLimiter:
    """Token bucket shared across every gateway replica."""

    def __init__(self, client: object, *, key_prefix: str = "distillserve:ratelimit") -> None:
        """Bind to an async Redis client.

        The client is typed as ``object`` so this module does not import redis
        at module scope; the gateway must start without it installed.
        """
        self._client = client
        self._prefix = key_prefix
        self._script: object | None = None

    @property
    def distributed(self) -> bool:
        """Shared across replicas."""
        return True

    async def check(
        self, tenant_id: str, *, requests_per_minute: int, cost: int = 1
    ) -> RateLimitDecision:
        """Run the Lua bucket script for ``tenant_id``."""
        if self._script is None:
            self._script = self._client.register_script(_REFILL_AND_CONSUME_LUA)  # type: ignore[attr-defined]

        # TTL is two full refills: long enough that an active tenant's bucket
        # never evaporates mid-burst, short enough that idle tenants expire
        # instead of accumulating keys forever.
        ttl_ms = 120_000
        allowed, remaining, retry_after_ms = await self._script(  # type: ignore[operator]
            keys=[f"{self._prefix}:{tenant_id}"],
            args=[
                requests_per_minute / 60.0,
                requests_per_minute,
                int(time.time() * 1000),
                cost,
                ttl_ms,
            ],
        )
        return RateLimitDecision(
            allowed=bool(allowed),
            remaining=int(remaining),
            retry_after_ms=int(retry_after_ms),
            limit=requests_per_minute,
            distributed=True,
        )
