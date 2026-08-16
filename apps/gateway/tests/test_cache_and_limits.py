"""Tests for the semantic cache and per-tenant rate limiting."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from distillserve_gateway.core.cache import (
    CachedResponse,
    InMemoryCacheStore,
    LexicalEmbedder,
    SemanticCache,
    cosine_similarity,
)
from distillserve_gateway.core.ratelimit import InMemoryRateLimiter


def a_response(text: str = "cached answer") -> CachedResponse:
    return CachedResponse(
        text=text,
        model="groq/llama-3.1-8b-instant",
        input_tokens=42,
        output_tokens=7,
        cost_usd=0.0001,
        finish_reason="stop",
        created_at=0.0,
    )


class ScriptedEmbedder:
    """Returns a preset vector per text, for exercising similarity thresholds."""

    def __init__(self, vectors: Mapping[str, Sequence[float]]) -> None:
        self._vectors = vectors

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def semantic(self) -> bool:
        return True

    async def embed(self, text: str) -> Sequence[float]:
        return self._vectors[text]


class BrokenEmbedder:
    """Always fails, to prove the cache degrades to a miss."""

    @property
    def name(self) -> str:
        return "broken"

    @property
    def semantic(self) -> bool:
        return True

    async def embed(self, text: str) -> Sequence[float]:
        raise RuntimeError("embedding endpoint unreachable")


# --- similarity ------------------------------------------------------------


def test_cosine_similarity_of_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_mismatched_dimensions_score_zero_rather_than_raising() -> None:
    """A dimension change on redeploy must invalidate hits, not crash the gateway."""
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


# --- lexical embedder ------------------------------------------------------


async def test_lexical_embedder_is_deterministic() -> None:
    embedder = LexicalEmbedder()
    assert list(await embedder.embed("hello world")) == list(await embedder.embed("hello world"))


async def test_lexical_embedder_normalises_whitespace_and_case() -> None:
    embedder = LexicalEmbedder()
    left = await embedder.embed("Summarize This Thread")
    right = await embedder.embed("summarize   this thread")

    assert cosine_similarity(left, right) == pytest.approx(1.0, abs=1e-9)


async def test_lexical_embedder_separates_unrelated_text() -> None:
    embedder = LexicalEmbedder()
    similarity = cosine_similarity(
        await embedder.embed("Summarize this support thread"),
        await embedder.embed("Write a SQL query for active users"),
    )
    assert similarity < 0.5


async def test_lexical_embedder_reports_itself_as_non_semantic() -> None:
    """The dashboard must be able to say 'lexical mode' rather than imply recall."""
    assert LexicalEmbedder().semantic is False


# --- cache behaviour -------------------------------------------------------


async def test_a_stored_response_is_served_back() -> None:
    cache = SemanticCache(LexicalEmbedder())
    await cache.store("Summarize this thread.", a_response(), tenant_id="t1", model="m1")

    lookup = await cache.lookup("Summarize this thread.", tenant_id="t1", model="m1")

    assert lookup.hit
    assert lookup.response is not None
    assert lookup.response.text == "cached answer"


async def test_a_dissimilar_prompt_misses() -> None:
    cache = SemanticCache(LexicalEmbedder())
    await cache.store("Summarize this thread.", a_response(), tenant_id="t1", model="m1")

    lookup = await cache.lookup("Write a SQL query for active users.", tenant_id="t1", model="m1")

    assert not lookup.hit


async def test_the_threshold_is_enforced() -> None:
    """A false hit answers a different question, so the bar is deliberately high."""
    vectors = {"a": [1.0, 0.0], "b": [0.9, 0.436]}  # cosine ~= 0.9
    strict = SemanticCache(ScriptedEmbedder(vectors), similarity_threshold=0.95)
    lenient = SemanticCache(ScriptedEmbedder(vectors), similarity_threshold=0.85)

    for cache in (strict, lenient):
        await cache.store("a", a_response(), tenant_id="t1", model="m1")

    assert not (await strict.lookup("b", tenant_id="t1", model="m1")).hit
    assert (await lenient.lookup("b", tenant_id="t1", model="m1")).hit


async def test_tenants_are_isolated() -> None:
    """Cross-tenant leakage would be a security failure, not a cache miss."""
    cache = SemanticCache(LexicalEmbedder())
    await cache.store("Summarize this.", a_response(), tenant_id="tenant-a", model="m1")

    assert not (await cache.lookup("Summarize this.", tenant_id="tenant-b", model="m1")).hit


async def test_models_are_isolated() -> None:
    """Serving the student's answer to a teacher route would destroy the comparison."""
    cache = SemanticCache(LexicalEmbedder())
    await cache.store("Summarize this.", a_response(), tenant_id="t1", model="student")

    assert not (await cache.lookup("Summarize this.", tenant_id="t1", model="teacher")).hit


async def test_a_disabled_cache_never_hits() -> None:
    cache = SemanticCache(LexicalEmbedder(), enabled=False)
    await cache.store("Summarize this.", a_response(), tenant_id="t1", model="m1")

    assert not (await cache.lookup("Summarize this.", tenant_id="t1", model="m1")).hit


async def test_an_embedder_failure_degrades_to_a_miss() -> None:
    """The cache is an optimisation; an unreachable one must not 500 the request."""
    cache = SemanticCache(BrokenEmbedder())

    assert not (await cache.lookup("anything", tenant_id="t1", model="m1")).hit
    await cache.store("anything", a_response(), tenant_id="t1", model="m1")  # must not raise


async def test_expired_entries_are_not_served() -> None:
    cache = SemanticCache(LexicalEmbedder(), ttl_seconds=1)
    await cache.store("Summarize this.", a_response(), tenant_id="t1", model="m1")
    await asyncio.sleep(1.05)

    assert not (await cache.lookup("Summarize this.", tenant_id="t1", model="m1")).hit


async def test_the_store_is_bounded() -> None:
    """An unbounded semantic cache is a memory leak with extra steps."""
    store = InMemoryCacheStore(max_entries=3)
    for index in range(10):
        await store.put("ns", f"k{index}", [float(index)], "payload", 60)

    assert len(await store.candidates("ns")) == 3


# --- rate limiting ---------------------------------------------------------


async def test_requests_are_allowed_up_to_the_limit() -> None:
    limiter = InMemoryRateLimiter()
    results = [await limiter.check("t1", requests_per_minute=5) for _ in range(5)]

    assert all(r.allowed for r in results)
    assert results[-1].remaining == 0


async def test_the_next_request_over_the_limit_is_refused() -> None:
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        await limiter.check("t1", requests_per_minute=5)

    decision = await limiter.check("t1", requests_per_minute=5)

    assert not decision.allowed
    assert decision.retry_after_ms > 0
    assert decision.retry_after_seconds >= 1


async def test_tenants_have_independent_buckets() -> None:
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        await limiter.check("noisy", requests_per_minute=5)

    assert (await limiter.check("quiet", requests_per_minute=5)).allowed


async def test_the_bucket_refills_over_time() -> None:
    """Continuous refill is why an idle tenant is not punished for a past burst."""
    limiter = InMemoryRateLimiter()
    for _ in range(60):
        await limiter.check("t1", requests_per_minute=60)

    assert not (await limiter.check("t1", requests_per_minute=60)).allowed
    await asyncio.sleep(1.05)  # 60/min == 1 token/second
    assert (await limiter.check("t1", requests_per_minute=60)).allowed


async def test_an_in_memory_limiter_admits_it_is_not_distributed() -> None:
    """Readiness reports this rather than implying a guarantee it cannot make."""
    decision = await InMemoryRateLimiter().check("t1", requests_per_minute=5)
    assert decision.distributed is False
