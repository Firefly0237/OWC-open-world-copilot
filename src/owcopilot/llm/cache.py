"""Client-side cache backends for the gateway — the layer that short-circuits a request
*before* any API call, so we save the whole call (tokens + latency), not just a discount.

Three pieces, all behind the `CacheBackend` protocol so the gateway is oblivious to which
is wired in:
  L1 ExactCache    — sha256 of (tier, system, user); catches byte-identical repeats.
  L2 SemanticCache — embedding nearest-neighbour; catches paraphrases L1 misses.
  LayeredCache     — tries L1 then L2 and promotes L2 hits up to L1.

(The third "cache" in P2 — DeepSeek's server-side prefix cache — is not implemented here;
it lives on the provider and is *measured* via telemetry's `cached_input_tokens`.)

Everything is offline/deterministic by default: the L2 embedder is dependency-injected and
the test/default `HashingEmbedder` needs no model or network.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CacheKey:
    """What the gateway hands a cache for one call. Each backend takes what it needs:
    ExactCache uses `.exact` (the full hash); SemanticCache embeds `.text` and scopes by `tier`."""

    tier: str
    system: str
    user: str

    @property
    def exact(self) -> str:
        return hashlib.sha256(f"{self.tier}\x00{self.system}\x00{self.user}".encode()).hexdigest()

    @property
    def scope(self) -> str:
        """Bucket key for L2: same tier + same system prompt. Two requests are candidates for a
        semantic match only if their grounding context (system) is identical — so a paraphrased
        intent that retrieves the same lore can hit, while a plan call (different system) or a
        request that pulls different lore never collides with a generate call."""
        return hashlib.sha256(f"{self.tier}\x00{self.system}".encode()).hexdigest()

    @property
    def text(self) -> str:
        """The semantically-meaningful text to embed for L2: the *request* (user message).

        We deliberately exclude the system prompt. In this design the system is derived from
        the request (retrieval-grounded lore) or is a constant stable prefix, so the user
        message is what actually distinguishes two requests. Embedding the whole prompt would
        let the long constant instruction + shared lore inflate similarity and collapse
        genuinely different requests onto each other.
        """
        return self.user


class CacheBackend(Protocol):
    def get(self, key: CacheKey) -> str | None: ...
    def set(self, key: CacheKey, value: str) -> None: ...


class NoOpCache:
    """Default in P0: never caches. Lets us measure the *un-optimised* baseline first."""

    def get(self, key: CacheKey) -> str | None:
        return None

    def set(self, key: CacheKey, value: str) -> None:
        return None


class ExactCache:
    """L1: in-memory exact-match cache keyed by the prompt's sha256.

    A hit returns the stored completion and the gateway never calls the provider. Swap the
    dict for Redis behind the same interface for a shared/persistent cache — the gateway
    doesn't change.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: CacheKey) -> str | None:
        return self._store.get(key.exact)

    def set(self, key: CacheKey, value: str) -> None:
        self._store[key.exact] = value


# --------------------------------------------------------------------------- L2 embeddings
class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


class HashingEmbedder:
    """Deterministic, offline, dependency-free embedder: a hashed bag-of-words vector.

    Prompts that share most words land near each other (high cosine); unrelated prompts
    don't. Good enough to exercise L2 in tests at $0. In production, inject a real
    sentence-transformer / provider embedding instead — the interface is identical.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokenize(text):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return vec


class SemanticCache:
    """L2: nearest-neighbour cache over request embeddings; catches paraphrases L1 misses.

    On `get`, embed the request (`key.text`) and return the stored value of the nearest entry
    *if* its cosine similarity clears `threshold` (and it shares the same tier). On a miss, the
    gateway will `set` it, growing the index. Linear scan is fine at copilot scale; swap in a
    vector DB behind the same interface if the index grows large.
    """

    def __init__(self, embedder: Embedder | None = None, *, threshold: float = 0.9) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.threshold = threshold
        self._entries: list[tuple[str, list[float], str]] = []  # (scope, vector, value)

    def get(self, key: CacheKey) -> str | None:
        q = self.embedder.embed(key.text)
        best_sim, best_val = 0.0, None
        for scope, vec, val in self._entries:
            if scope != key.scope:  # only compare within identical tier+context
                continue
            sim = cosine(q, vec)
            if sim > best_sim:
                best_sim, best_val = sim, val
        return best_val if best_sim >= self.threshold else None

    def set(self, key: CacheKey, value: str) -> None:
        self._entries.append((key.scope, self.embedder.embed(key.text), value))


class LayeredCache:
    """Compose backends (L1 -> L2 -> ...): first hit wins; an L2 hit is promoted into L1 so
    the next identical request is an O(1) exact hit. `set` writes through every layer."""

    def __init__(self, layers: list[CacheBackend]) -> None:
        self.layers = layers

    def get(self, key: CacheKey) -> str | None:
        for i, layer in enumerate(self.layers):
            val = layer.get(key)
            if val is not None:
                for upper in self.layers[:i]:  # promote into faster layers
                    upper.set(key, val)
                return val
        return None

    def set(self, key: CacheKey, value: str) -> None:
        for layer in self.layers:
            layer.set(key, value)


class RedisCache:
    """Shared exact-match cache backed by Redis.

    This is the multi-instance analogue of `ExactCache`: the key is the prompt's stable sha256 and
    the stored value is the completion text. The client is lazy-created so offline/default paths do
    not need the `redis` package installed; tests inject a fake Redis client directly.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        url: str = "redis://127.0.0.1:6379/0",
        prefix: str = "owcopilot:l1:",
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self.url = url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def get(self, key: CacheKey) -> str | None:
        value = self._conn().get(self.prefix + key.exact)
        return value if isinstance(value, str) else None

    def set(self, key: CacheKey, value: str) -> None:
        conn = self._conn()
        redis_key = self.prefix + key.exact
        if self.ttl_seconds is None:
            conn.set(redis_key, value)
        else:
            conn.setex(redis_key, self.ttl_seconds, value)

    def _conn(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client


def build_cache_backend(
    mode: str,
    *,
    semantic_threshold: float = 0.9,
    redis_url: str = "redis://127.0.0.1:6379/0",
    redis_client: Any | None = None,
    redis_ttl_seconds: int | None = None,
) -> CacheBackend:
    """Factory for the cache backends used by benchmark and service assembly."""
    if mode == "off":
        return NoOpCache()
    if mode == "exact":
        return ExactCache()
    if mode == "exact+semantic":
        return LayeredCache([ExactCache(), SemanticCache(threshold=semantic_threshold)])
    if mode == "redis":
        return RedisCache(redis_client, url=redis_url, ttl_seconds=redis_ttl_seconds)
    if mode == "redis+semantic":
        return LayeredCache(
            [
                RedisCache(redis_client, url=redis_url, ttl_seconds=redis_ttl_seconds),
                SemanticCache(threshold=semantic_threshold),
            ]
        )
    raise ValueError(f"unknown cache mode: {mode!r}")
