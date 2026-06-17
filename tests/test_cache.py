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


def test_hashing_embedder_semantic_cache_is_dead_for_cjk():
    # documents the gap that motivated injecting a real embedder: the hashing embedder tokenizes
    # [a-z0-9]+, so two (even identical-meaning) Chinese requests embed to empty vectors and never
    # semantically match — L2 is useless for CJK content with the default stub.
    c = SemanticCache(HashingEmbedder(), threshold=0.85)
    c.set(CacheKey("cheap", "SYS", "护送商队穿过雾脊山道"), "QUEST")
    # a would-be paraphrase still misses — CJK never reaches the bag-of-words vector
    assert c.get(CacheKey("cheap", "SYS", "护送商队走雾脊山道")) is None


class _StubSemanticEmbedder:
    """A real-model stand-in (model_id 'st:*') that DOES see CJK: same gambling/escort topic →
    same axis, so a Chinese paraphrase lands a semantic hit the hashing stub would miss."""

    model_id = "st:stub"

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "护送商队" in t else [0.0, 1.0] for t in texts]


def test_semantic_cache_with_real_embedder_hits_cjk_paraphrase():
    cache = build_cache_backend(
        "exact+semantic", embedder=_StubSemanticEmbedder(), semantic_threshold=0.9
    )
    cache.set(CacheKey("cheap", "SYS", "护送商队穿过雾脊山道"), "QUEST")
    # different wording, same meaning, same grounding → now a hit (L2 sees CJK)
    assert cache.get(CacheKey("cheap", "SYS", "护送商队走另一条路")) == "QUEST"
    # different topic, same grounding → still a miss
    assert cache.get(CacheKey("cheap", "SYS", "米拉在河湾治疗村民")) is None


def test_build_cache_backend_supports_redis_plus_semantic():
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = build_cache_backend("redis+semantic", redis_client=client, semantic_threshold=0.85)

    base = CacheKey("cheap", "SYS", "escort the caravan to Northwatch for Aldric")
    cache.set(base, "QUEST")
    paraphrase = CacheKey("cheap", "SYS", "for Aldric escort the caravan to Northwatch please")
    assert cache.get(paraphrase) == "QUEST"


# ----------------------------------------------------- cache key captures model + project scope
class _ModelProvider:
    """Fake real-provider stand-in that carries a `.model` id (like OpenAICompatProvider) and
    returns a model-specific completion, so we can prove the cache distinguishes models."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    def complete(self, *, system, user, model):
        self.calls += 1
        return f"{self.model}-resp", 10, 5


def test_gateway_cache_key_includes_model_id():
    # Two gateways share ONE cache but front different models on the same tier. A request to the
    # second model must NOT be served the first model's cached completion (regression: the key
    # omitted the model id, so switching llm_model silently returned the wrong model's answer).
    cache = ExactCache()
    flash = _ModelProvider("deepseek-v4-flash")
    pro = _ModelProvider("deepseek-v4-pro")
    gw_flash = LLMGateway(providers={"cheap": flash}, cache=cache)
    gw_pro = LLMGateway(providers={"cheap": pro}, cache=cache)

    a = gw_flash.complete(task="qa_answer", system="s", user="u", tier="cheap")
    b = gw_pro.complete(task="qa_answer", system="s", user="u", tier="cheap")

    assert a == "deepseek-v4-flash-resp"
    assert b == "deepseek-v4-pro-resp"  # pro was actually called, not served flash's cache
    assert pro.calls == 1


def test_gateway_cache_key_scopes_by_namespace():
    # One shared cache, two projects (namespaces), same model + prompt. Project B must not be served
    # project A's completion (regression: the action/service cache had no project dimension).
    cache = ExactCache()
    prov_a = _ModelProvider("m")
    prov_b = _ModelProvider("m")
    gw_a = LLMGateway(providers={"cheap": prov_a}, cache=cache, namespace="projA")
    gw_b = LLMGateway(providers={"cheap": prov_b}, cache=cache, namespace="projB")

    gw_a.complete(task="world_seed", system="s", user="u", tier="cheap")
    gw_b.complete(task="world_seed", system="s", user="u", tier="cheap")
    assert prov_a.calls == 1 and prov_b.calls == 1  # neither served the other's entry

    gw_a.complete(task="world_seed", system="s", user="u", tier="cheap")
    assert prov_a.calls == 1  # ...but same project+model+prompt still hits (caching not broken)


def test_cache_key_exact_and_scope_distinguish_model_and_namespace():
    base = CacheKey("cheap", "sys", "u")
    assert CacheKey("cheap", "sys", "u", model="x").exact != base.exact
    assert CacheKey("cheap", "sys", "u", namespace="p").exact != base.exact
    assert CacheKey("cheap", "sys", "u", model="x").scope != base.scope
    assert CacheKey("cheap", "sys", "u", namespace="p").scope != base.scope
