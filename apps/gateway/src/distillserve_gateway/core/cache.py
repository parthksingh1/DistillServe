"""Semantic response cache.

An exact-match cache is nearly useless in front of an LLM: production prompts
are templated with varying context, so byte-identical repeats are rare. A
semantic cache stores the prompt's embedding and serves a stored response when
a new prompt is close enough — which is what actually turns repeated support
questions, retried requests and dashboard refreshes into zero-cost hits.

Three deliberate choices:

**Similarity threshold is high (0.95 by default) and configurable.** A false
hit returns a confidently wrong answer to a different question, which is far
worse than a miss. Cache hits are cheap to lose and expensive to get wrong.

**The cache key is scoped by tenant and model.** Two tenants must never see
each other's responses, and the student's answer must never be served to a
request routed to the teacher — that would silently destroy the quality
comparison the whole platform is built on.

**Embedding is pluggable, and none of the defaults need a GPU or a 2 GB
download.** ``HostedEmbedder`` calls the configured provider's embedding
endpoint; ``LexicalEmbedder`` is a dependency-free character n-gram hash used
when no provider is available. The ``ml`` extra adds a local
sentence-transformers embedder for deployments that want one.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from distillserve_otel import get_logger

log = get_logger(__name__)

#: Dimensionality of the lexical fallback embedding. 512 buckets is enough to
#: keep unrelated prompts near-orthogonal without making every vector large.
_LEXICAL_DIM = 512


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into a vector."""

    @property
    def name(self) -> str:
        """Embedder identifier, recorded on the trace."""
        ...

    @property
    def semantic(self) -> bool:
        """True when the vector captures meaning rather than surface form."""
        ...

    async def embed(self, text: str) -> Sequence[float]:
        """Return a unit-normalised embedding of ``text``."""
        ...


def _normalise(vector: list[float]) -> list[float]:
    """Scale a vector to unit length so cosine similarity is a dot product."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors, clamped to ``[-1, 1]``."""
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return max(-1.0, min(1.0, dot))


class LexicalEmbedder:
    """Dependency-free character n-gram hashing embedder.

    Catches near-duplicates — retries, whitespace differences, a changed
    pleasantry — but not paraphrases. It reports ``semantic = False`` so
    readiness and the dashboard can say plainly that the cache is running in
    lexical mode rather than implying semantic recall it does not have.
    """

    def __init__(self, dimensions: int = _LEXICAL_DIM, ngram: int = 4) -> None:
        """Configure the hash space and n-gram width."""
        self._dimensions = dimensions
        self._ngram = ngram

    @property
    def name(self) -> str:
        """Embedder identifier."""
        return "lexical-hash"

    @property
    def semantic(self) -> bool:
        """Surface-form only."""
        return False

    async def embed(self, text: str) -> Sequence[float]:
        """Hash character n-grams into a fixed-width bag-of-features vector."""
        normalised = re.sub(r"\s+", " ", text.strip().lower())
        vector = [0.0] * self._dimensions
        if not normalised:
            return vector

        for index in range(max(1, len(normalised) - self._ngram + 1)):
            gram = normalised[index : index + self._ngram]
            bucket = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=4).digest(), "big")
            vector[bucket % self._dimensions] += 1.0
        return _normalise(vector)


class HostedEmbedder:
    """Embeddings from the configured provider, via LiteLLM."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        """Bind to an embedding model id, e.g. ``gemini/text-embedding-004``."""
        self._model = model
        self._api_key = api_key

    @property
    def name(self) -> str:
        """Embedder identifier."""
        return self._model

    @property
    def semantic(self) -> bool:
        """Real embeddings, so paraphrases hit."""
        return True

    async def embed(self, text: str) -> Sequence[float]:
        """Call the provider's embedding endpoint.

        Imported lazily: the cache must be constructible in tests and in
        environments where the provider is unreachable.
        """
        import litellm

        kwargs: dict[str, Any] = {"model": self._model, "input": [text]}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        response = await litellm.aembedding(**kwargs)
        vector = list(response["data"][0]["embedding"])
        return _normalise([float(value) for value in vector])


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """A stored completion and what it cost to produce the first time."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    finish_reason: str
    created_at: float

    def to_json(self) -> str:
        """Serialise for storage."""
        return json.dumps(
            {
                "text": self.text,
                "model": self.model,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd,
                "finish_reason": self.finish_reason,
                "created_at": self.created_at,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str) -> CachedResponse:
        """Deserialise from storage."""
        data = json.loads(payload)
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """The outcome of a cache lookup."""

    hit: bool
    response: CachedResponse | None = None
    similarity: float = 0.0
    embedder: str = ""
    semantic: bool = False


@runtime_checkable
class CacheStore(Protocol):
    """Storage for embeddings and their responses."""

    async def candidates(self, namespace: str) -> Sequence[tuple[Sequence[float], str]]:
        """Return ``(embedding, payload)`` pairs in ``namespace``."""
        ...

    async def put(
        self, namespace: str, key: str, embedding: Sequence[float], payload: str, ttl_seconds: int
    ) -> None:
        """Store an entry."""
        ...


class InMemoryCacheStore:
    """Bounded LRU store for a single process.

    Bounded because an unbounded semantic cache is a memory leak with extra
    steps: entries are never invalidated by a key, only aged out.
    """

    def __init__(self, max_entries: int = 512) -> None:
        """Set the per-namespace entry ceiling."""
        self._max = max_entries
        self._data: dict[str, OrderedDict[str, tuple[Sequence[float], str, float]]] = {}

    async def candidates(self, namespace: str) -> Sequence[tuple[Sequence[float], str]]:
        """Return live entries, dropping any that have expired."""
        bucket = self._data.get(namespace)
        if not bucket:
            return ()
        now = time.time()
        expired = [key for key, (_, _, expires) in bucket.items() if expires <= now]
        for key in expired:
            del bucket[key]
        return [(embedding, payload) for embedding, payload, _ in bucket.values()]

    async def put(
        self, namespace: str, key: str, embedding: Sequence[float], payload: str, ttl_seconds: int
    ) -> None:
        """Store an entry, evicting the least recently used when full."""
        bucket = self._data.setdefault(namespace, OrderedDict())
        bucket[key] = (embedding, payload, time.time() + ttl_seconds)
        bucket.move_to_end(key)
        while len(bucket) > self._max:
            bucket.popitem(last=False)


class SemanticCache:
    """Embedding-similarity cache in front of the provider."""

    def __init__(
        self,
        embedder: Embedder,
        store: CacheStore | None = None,
        *,
        similarity_threshold: float = 0.95,
        ttl_seconds: int = 3_600,
        enabled: bool = True,
    ) -> None:
        """Configure the cache.

        Args:
            embedder: Turns prompts into vectors.
            store: Where entries live. Defaults to a bounded in-process store.
            similarity_threshold: Minimum cosine similarity for a hit. High by
                default because a false hit answers the wrong question.
            ttl_seconds: How long an entry stays servable.
            enabled: Master switch, so the cache can be turned off per
                deployment without removing it from the pipeline.
        """
        self._embedder = embedder
        self._store = store or InMemoryCacheStore()
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether lookups are performed."""
        return self._enabled

    @property
    def semantic(self) -> bool:
        """Whether the active embedder captures meaning."""
        return self._embedder.semantic

    @staticmethod
    def namespace(tenant_id: str, model: str) -> str:
        """Build the isolation key for a lookup.

        Tenant *and* model: cross-tenant leakage is a security failure, and
        serving the student's answer to a teacher-routed request would silently
        destroy the quality comparison the platform exists to make.
        """
        return f"{tenant_id}:{model}"

    async def lookup(self, prompt: str, *, tenant_id: str, model: str) -> CacheLookup:
        """Find a sufficiently similar cached response.

        A failure here is never fatal: the cache is an optimisation, and an
        unreachable store must degrade to a miss rather than a 500.
        """
        if not self._enabled:
            return CacheLookup(hit=False, embedder=self._embedder.name)

        try:
            embedding = await self._embedder.embed(prompt)
            entries = await self._store.candidates(self.namespace(tenant_id, model))
        except Exception as exc:
            log.warning("cache.lookup_failed", error=str(exc), embedder=self._embedder.name)
            return CacheLookup(hit=False, embedder=self._embedder.name)

        best_similarity = 0.0
        best_payload: str | None = None
        for candidate, payload in entries:
            similarity = cosine_similarity(embedding, candidate)
            if similarity > best_similarity:
                best_similarity, best_payload = similarity, payload

        if best_payload is None or best_similarity < self._threshold:
            return CacheLookup(
                hit=False,
                similarity=best_similarity,
                embedder=self._embedder.name,
                semantic=self._embedder.semantic,
            )

        return CacheLookup(
            hit=True,
            response=CachedResponse.from_json(best_payload),
            similarity=best_similarity,
            embedder=self._embedder.name,
            semantic=self._embedder.semantic,
        )

    async def store(
        self, prompt: str, response: CachedResponse, *, tenant_id: str, model: str
    ) -> None:
        """Cache a completion under the tenant/model namespace."""
        if not self._enabled:
            return
        try:
            embedding = await self._embedder.embed(prompt)
            key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]
            await self._store.put(
                self.namespace(tenant_id, model), key, embedding, response.to_json(), self._ttl
            )
        except Exception as exc:
            log.warning("cache.store_failed", error=str(exc))
