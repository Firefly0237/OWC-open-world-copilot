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
import threading
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CacheKey:
    """What the gateway hands a cache for one call. Each backend takes what it needs:
    ExactCache uses `.exact` (the full hash); SemanticCache embeds `.text` and scopes by `.scope`.

    The key captures *everything that determines the completion*: not just the prompt
    (`tier`/`system`/`user`) but also which `model` produced it and which `namespace` (project) it
    belongs to. Omitting either silently serves one project — or one model — the answer computed for
    another: a request that explicitly switches `llm_model` (or asks the same question in another
    project) must never be short-circuited by a stale entry.
    """

    tier: str
    system: str
    user: str
    namespace: str = ""  # project / content-root scope; "" = un-scoped (single-project) default
    model: str = ""  # the real model id behind the tier (a different model must miss, not reuse)

    @property
    def exact(self) -> str:
        parts = (self.namespace, self.model, self.tier, self.system, self.user)
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    @property
    def scope(self) -> str:
        """Bucket key for L2: same namespace + model + tier + system prompt. Two requests are
        candidates for a semantic match only if they share the same project, the same model, and an
        identical grounding context (system) — so a paraphrased intent that retrieves the same lore
        can hit, while a plan call (different system), a request that pulls different lore, a
        different model, or another project never collides with a generate call."""
        parts = (self.namespace, self.model, self.tier, self.system)
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

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
        # One shared instance backs every request thread (FastAPI runs sync endpoints in a
        # threadpool); guard the dict so concurrent get/set never race.
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> str | None:
        with self._lock:
            return self._store.get(key.exact)

    def set(self, key: CacheKey, value: str) -> None:
        with self._lock:
            self._store[key.exact] = value


# --------------------------------------------------------------------------- L2 embeddings
class Embedder(Protocol):
    # A short, stable id of the embedding space; the vector retriever keys its persisted
    # cache on it so swapping models never mixes incompatible vectors.
    model_id: str

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Neural backends override this for batched throughput."""
        ...


# Pre-compiled regex: Latin/digit words OR individual CJK characters.
# CJK blocks covered:
#   [一-鿿]  — CJK Unified Ideographs (U+4E00–U+9FFF) + Ext A (U+3400–U+4DBF)
#   [㐀-䶿]  — CJK Extension A overflow range (U+3400–U+4DBF; kept for clarity)
#   [豈-﫿]  — CJK Compatibility Ideographs and other CJK-adjacent blocks (U+F900–U+FAFF)
#   [𠀀-𯨟]  — CJK Extension B (supplementary plane U+20000–U+2A6DF); written as surrogate pair
# All supplementary-plane CJK characters beyond U+FFFF are intentionally omitted here because
# Python str iterates over codepoints and re already handles them correctly with the \U escape,
# but keeping the regex ASCII-safe makes it easier to read/audit. The main Unified block
# (U+4E00–U+9FFF) covers the vast majority of simplified & traditional Chinese usage.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿㐀-䶿豈-﫿]")


def _tokenize(text: str) -> list[str]:
    """Unicode-aware tokenizer for the hashing embedder.

    * **Latin / digit tokens** — same as before: ``[a-z0-9]+`` substrings of the lowercased
      text (e.g. ``"escort the caravan"`` → ``["escort", "the", "caravan"]``).
    * **CJK characters** — each CJK character is emitted as an individual token *and* each pair
      of adjacent CJK characters is emitted as a bigram (sliding-window, step 1).  The bigrams
      add an order-sensitive signal so that Chinese paraphrases that share most characters land
      close to each other in the hashed vector space, while completely different topics don't.

    Example::

        _tokenize("护送商队穿过雾脊山道")
        # → ['护', '送', '商', '队', '穿', '过', '雾', '脊', '山', '道',
        #     '护送', '送商', '商队', '队穿', '穿过', '过雾', '雾脊', '脊山', '山道']

        _tokenize("escort the caravan")
        # → ['escort', 'the', 'caravan']   (unchanged Latin behaviour)

    Mixed text (e.g. ``"护送商队 escort"`` ) produces tokens from both CJK and Latin branches.
    English path is fully backward-compatible: the extra branch fires only on CJK codepoints.
    """
    chars = _TOKEN_RE.findall(text.lower())
    result = list(chars)
    for i in range(len(chars) - 1):
        # Both adjacent tokens must be single-character CJK to form a bigram.
        # Latin/digit tokens have length > 1 (words), so len(...) == 1 uniquely identifies
        # single CJK chars without an extra character-class check.
        if len(chars[i]) == 1 and len(chars[i + 1]) == 1:
            result.append(chars[i] + chars[i + 1])
    return result


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


class HashingEmbedder:
    """Deterministic, offline, dependency-free embedder: a hashed bag-of-characters/words vector.

    Prompts that share most tokens land near each other (high cosine); unrelated prompts don't.
    The underlying ``_tokenize`` function is CJK-aware: CJK characters are emitted as individual
    character tokens plus adjacent-pair bigrams, so Chinese paraphrases produce overlapping token
    sets and land close in the vector space.  Latin/digit text is tokenized as whole words
    (``[a-z0-9]+``), same as before.  Good enough to exercise L2 in tests at $0 — no model or
    network needed.  In production, inject a real sentence-transformer / provider embedding
    instead — the interface is identical.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.model_id = f"hashing-{dim}"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokenize(text):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return vec

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


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
        # The index is shared across request threads; guard mutation so a concurrent set() can't
        # grow the list mid-scan (which would raise "list changed size during iteration").
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> str | None:
        q = self.embedder.embed(key.text)  # embed outside the lock — a neural backend is slow
        with self._lock:
            entries = tuple(self._entries)  # cheap snapshot of references; scan it lock-free
        best_sim, best_val = 0.0, None
        for scope, vec, val in entries:
            if scope != key.scope:  # only compare within identical project+model+tier+context
                continue
            sim = cosine(q, vec)
            if sim > best_sim:
                best_sim, best_val = sim, val
        return best_val if best_sim >= self.threshold else None

    def set(self, key: CacheKey, value: str) -> None:
        vec = self.embedder.embed(key.text)  # embed outside the lock
        with self._lock:
            self._entries.append((key.scope, vec, value))


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
        self._client_lock = threading.Lock()

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
            with self._client_lock:
                if self._client is None:  # double-checked: only one thread builds the client
                    import redis

                    self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client


def build_cache_backend(
    mode: str,
    *,
    semantic_threshold: float = 0.9,
    embedder: Embedder | None = None,
    redis_url: str = "redis://127.0.0.1:6379/0",
    redis_client: Any | None = None,
    redis_ttl_seconds: int | None = None,
) -> CacheBackend:
    """Factory for the cache backends the service hangs off the gateway.

    ``embedder`` backs the L2 semantic layer.  The default ``HashingEmbedder`` is CJK-aware
    (char + char-bigram tokenization), so L2 also works for Chinese/Japanese/Korean text out of
    the box at $0.  For higher semantic accuracy, pass a real multilingual model (e.g. bge-m3
    via ``SemanticEmbedder``) — the interface is identical and the model path takes priority when
    ``[semantic]`` extras are installed."""
    if mode == "off":
        return NoOpCache()
    if mode == "exact":
        return ExactCache()
    if mode == "exact+semantic":
        return LayeredCache([ExactCache(), SemanticCache(embedder, threshold=semantic_threshold)])
    if mode == "redis":
        return RedisCache(redis_client, url=redis_url, ttl_seconds=redis_ttl_seconds)
    if mode == "redis+semantic":
        return LayeredCache(
            [
                RedisCache(redis_client, url=redis_url, ttl_seconds=redis_ttl_seconds),
                SemanticCache(embedder, threshold=semantic_threshold),
            ]
        )
    raise ValueError(f"unknown cache mode: {mode!r}")
