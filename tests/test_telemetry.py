"""Cost model + provider-cache accounting (P2 T2).

The cost formula prices server-cached input tokens (hit) separately from fresh input
tokens (miss) — verified here against a hand calculation.
"""

from owcopilot.llm.telemetry import PRICES, CallRecord, TelemetryCollector


def test_cost_splits_cache_hit_and_miss_input_pricing():
    hit_p, miss_p, out_p = PRICES["cheap"]
    rec = CallRecord(
        task="generate", tier="cheap", input_tokens=1000, output_tokens=200, cached_input_tokens=400
    )
    # 400 input tokens at the cheap cache-hit rate, 600 at the miss rate, 200 output.
    expected = (400 * hit_p + 600 * miss_p + 200 * out_p) / 1_000_000
    assert rec.cost_usd == expected
    # hit price must be well below miss price (that's the whole point of prefix caching).
    assert hit_p < miss_p


def test_cached_tokens_never_exceed_input():
    hit_p = PRICES["cheap"][0]
    rec = CallRecord(
        task="x", tier="cheap", input_tokens=100, output_tokens=0, cached_input_tokens=500
    )  # nonsensical over-report
    assert rec.cost_usd == (100 * hit_p) / 1_000_000  # clamped: all 100 treated as hits, 0 miss


def test_client_cache_hit_record_is_zero_cost():
    rec = CallRecord(
        task="generate", tier="frontier", input_tokens=0, output_tokens=0, cache_hit=True
    )
    assert rec.cost_usd == 0.0


def test_provider_cache_hit_token_share():
    tel = TelemetryCollector()
    tel.record(
        CallRecord("generate", "cheap", input_tokens=1000, output_tokens=0, cached_input_tokens=400)
    )
    tel.record(
        CallRecord("generate", "cheap", input_tokens=1000, output_tokens=0, cached_input_tokens=600)
    )
    assert tel.provider_cache_hit_tokens == 1000
    assert tel.provider_cache_hit_token_share == 0.5  # 1000 hit / 2000 input
    assert tel.cache_hit_rate == 0.0  # these are provider hits, not client hits


def test_summary_has_p2_metrics():
    tel = TelemetryCollector()
    tel.record(CallRecord("generate", "cheap", input_tokens=100, output_tokens=10))
    s = tel.summary()
    for key in (
        "calls",
        "input_tokens",
        "output_tokens",
        "cache_hit_rate",
        "provider_cache_hit_token_share",
        "mean_latency_ms",
        "total_cost_usd",
    ):
        assert key in s
