"""Client-side caches (P2 T3/T4): L1 exact, L2 semantic, and the layered composite.

All offline/$0: the L2 embedder is the deterministic HashingEmbedder, and a counting fake
provider proves a cache hit means *no* provider call happened.
"""

import pytest

from owcopilot.llm.cache import (
    CacheKey,
    ExactCache,
    HashingEmbedder,
    LayeredCache,
    NoOpCache,
    RedisCache,
    SemanticCache,
    build_cache_backend,
)
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.telemetry import TelemetryCollector


class _CountingProvider:
    """Fake provider that counts how many times it is actually called."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, model):
        self.calls += 1
        return f"resp-{self.calls}", 10, 5


# --------------------------------------------------------------------- T3 ExactCache
def test_exact_cache_short_circuits_identical_call_via_gateway():
    prov = _CountingProvider()
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": prov}, cache=ExactCache(), telemetry=tel)

    a = gw.complete(task="plan", system="s", user="u", tier="cheap")
    b = gw.complete(task="plan", system="s", user="u", tier="cheap")

    assert a == b  # same value returned
    assert prov.calls == 1  # second call served from cache, no new provider call
    assert tel.records[1].cache_hit is True
    assert tel.records[1].input_tokens == 0 and tel.records[1].output_tokens == 0
    assert tel.records[1].cost_usd == 0.0  # client hit is free


def test_exact_cache_keys_on_full_prompt():
    c = ExactCache()
    k = CacheKey("cheap", "sys", "hello")
    c.set(k, "v1")
    assert c.get(k) == "v1"
    assert c.get(CacheKey("cheap", "sys", "different")) is None  # different user
    assert c.get(CacheKey("frontier", "sys", "hello")) is None  # different tier


# --------------------------------------------------------------------- T4 SemanticCache
def test_semantic_cache_hits_paraphrase_misses_different():
    c = SemanticCache(HashingEmbedder(), threshold=0.85)
    base = CacheKey("cheap", "SYS", "escort the caravan to Northwatch for Aldric")
    c.set(base, "QUEST")

    paraphrase = CacheKey("cheap", "SYS", "for Aldric escort the caravan to Northwatch please")
    assert c.get(paraphrase) == "QUEST"  # reorder + filler -> near-identical bag of words

    different = CacheKey("cheap", "SYS", "Mira heals wounded villagers in Riverbend")
    assert c.get(different) is None  # shares no content -> below threshold


def test_semantic_cache_one_miss_one_hit_via_gateway():
    prov = _CountingProvider()
    tel = TelemetryCollector()
    gw = LLMGateway(
        providers={"cheap": prov},
        cache=SemanticCache(HashingEmbedder(), threshold=0.85),
        telemetry=tel,
    )

    gw.complete(
        task="generate",
        system="SYS",
        user="escort the caravan to Northwatch for Aldric",
        tier="cheap",
    )  # miss
    gw.complete(
        task="generate",
        system="SYS",
        user="for Aldric escort the caravan to Northwatch please",
        tier="cheap",
    )  # hit

    assert prov.calls == 1
    assert tel.records[1].cache_hit is True


def test_semantic_cache_respects_context_scope():
    # Identical request text but a DIFFERENT system context must not collide (scope guard).
    c = SemanticCache(HashingEmbedder(), threshold=0.85)
    c.set(CacheKey("cheap", "SYS-A", "escort the caravan to Northwatch for Aldric"), "A")
    assert c.get(CacheKey("cheap", "SYS-B", "escort the caravan to Northwatch for Aldric")) is None


# --------------------------------------------------------------------- LayeredCache
def test_layered_cache_promotes_semantic_hit_into_exact():
    l1, l2 = ExactCache(), SemanticCache(HashingEmbedder(), threshold=0.85)
    layered = LayeredCache([l1, l2])
    base = CacheKey("cheap", "SYS", "escort the caravan to Northwatch for Aldric")
    layered.set(base, "QUEST")

    paraphrase = CacheKey("cheap", "SYS", "for Aldric escort the caravan to Northwatch please")
    assert layered.get(paraphrase) == "QUEST"  # served by L2
    assert l1.get(paraphrase) == "QUEST"  # ...and promoted into L1 for next time


def test_noop_cache_never_hits():
    c = NoOpCache()
    k = CacheKey("cheap", "s", "u")
    c.set(k, "v")
    assert c.get(k) is None


# --------------------------------------------------------------------- RedisCache
def test_redis_cache_round_trips_via_fakeredis():
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = RedisCache(client=client, prefix="test:")
    key = CacheKey("cheap", "SYS", "escort the caravan")

    assert cache.get(key) is None
    cache.set(key, "QUEST")
    assert cache.get(key) == "QUEST"


def test_build_cache_backend_supports_redis_plus_semantic():
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = build_cache_backend("redis+semantic", redis_client=client, semantic_threshold=0.85)

    base = CacheKey("cheap", "SYS", "escort the caravan to Northwatch for Aldric")
    cache.set(base, "QUEST")
    paraphrase = CacheKey("cheap", "SYS", "for Aldric escort the caravan to Northwatch please")
    assert cache.get(paraphrase) == "QUEST"
