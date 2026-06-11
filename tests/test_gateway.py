from owcopilot.llm.gateway import LLMGateway, LLMGatewayError, MockProvider, OpenAICompatProvider
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import PRICES, TelemetryCollector


def test_routing_and_telemetry():
    tel = TelemetryCollector()
    gw = LLMGateway(
        providers={"cheap": MockProvider(), "frontier": MockProvider()},
        router=StaticRouter(),
        telemetry=tel,
    )

    gw.complete(task="plan", system="s", user="u")  # -> cheap
    gw.complete(task="generate", system="s", user="u")  # -> frontier

    assert tel.records[0].tier == "cheap"
    assert tel.records[1].tier == "frontier"
    assert tel.total_input_tokens > 0
    assert tel.cache_hit_rate == 0.0  # NoOpCache never hits in P0
    assert tel.total_cost > 0  # frontier tier has non-zero price


class _CacheReportingProvider:
    """Real-provider stand-in that returns the optional 4th value: the server-side
    prompt_cache_hit_tokens (a subset of the input tokens)."""

    def complete(self, *, system, user, model):
        return "ok", 1000, 50, 400  # 400 of the 1000 input tokens were cache hits


def test_gateway_passes_through_provider_cache_hit_tokens():
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": _CacheReportingProvider()}, telemetry=tel)
    gw.complete(task="generate", system="s", user="u", tier="cheap")

    rec = tel.records[0]
    assert rec.input_tokens == 1000
    assert rec.cached_input_tokens == 400  # the 4-tuple's hit tokens were recorded
    hit_p, miss_p, out_p = PRICES["cheap"]
    assert rec.cost_usd == (400 * hit_p + 600 * miss_p + 50 * out_p) / 1_000_000


def test_gateway_tolerates_three_tuple_provider():
    # Providers that don't report cache tokens (the offline fakes) still work; cached == 0.
    tel = TelemetryCollector()
    gw = LLMGateway(providers={"cheap": MockProvider()}, telemetry=tel)
    gw.complete(task="plan", system="s", user="u", tier="cheap")
    assert tel.records[0].cached_input_tokens == 0


def test_gateway_does_not_cache_empty_response():
    # A transient empty completion must not be memoized, so a retry is a genuine fresh call.
    from owcopilot.llm.cache import ExactCache

    class _EmptyThenX:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            return ("" if self.calls == 1 else "x"), 1, 1

    prov = _EmptyThenX()
    gw = LLMGateway(providers={"cheap": prov}, cache=ExactCache())
    a = gw.complete(task="generate", system="s", user="u", tier="cheap")
    b = gw.complete(task="generate", system="s", user="u", tier="cheap")
    assert a == "" and b == "x"  # empty not cached -> second call re-invokes provider
    assert prov.calls == 2


def test_openai_provider_gates_json_mode_on_prompt_mentioning_json():
    # DeepSeek/OpenAI 400 if response_format=json_object but the prompt has no 'json'.
    # The planner prompt has none (and is discarded); generate/repair prompts do.
    p = OpenAICompatProvider(model="deepseek-v4-flash")  # json_mode=True default
    assert p._wants_json("Return ONE JSON object with keys: …", "make a quest") is True
    assert p._wants_json("You are a planner.", "Decompose into steps: foo") is False
    assert OpenAICompatProvider(model="x", json_mode=False)._wants_json("Return JSON", "x") is False


def test_gateway_retries_transient_provider_error():
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return "ok", 1, 1

    prov = _Flaky()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=1)
    assert gw.complete(task="plan", system="s", user="u", tier="cheap") == "ok"
    assert prov.calls == 2


def test_gateway_classifies_provider_error_after_retries_exhausted():
    class _AlwaysTimeout:
        def complete(self, *, system, user, model):
            raise TimeoutError("timed out")

    gw = LLMGateway(providers={"cheap": _AlwaysTimeout()}, max_retries=1)
    try:
        gw.complete(task="plan", system="s", user="u", tier="cheap")
    except LLMGatewayError as e:
        assert e.category == "timeout"
        assert e.attempts == 2
    else:
        raise AssertionError("expected LLMGatewayError")
