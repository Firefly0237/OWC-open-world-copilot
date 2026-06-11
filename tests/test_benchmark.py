"""Benchmark harness (P2 T1): the offline 'ruler'. Deterministic, $0.

Asserts the BenchmarkResult is well-formed, that turning the cache ON yields client hits on
the repeated/paraphrased intents, and that ON beats OFF on cost without lowering first-pass
consistency (the quality floor the optimisation must not sacrifice).
"""

from owcopilot.benchmark import OFF, ON, BenchmarkResult, compare, run_benchmark


def test_benchmark_offline_runs_and_reports_all_metrics():
    r = run_benchmark(config=ON)
    assert isinstance(r, BenchmarkResult)
    assert r.n_intents > 0
    assert r.total_cost_usd >= 0.0
    assert isinstance(r.input_tokens, int) and r.input_tokens > 0
    assert isinstance(r.output_tokens, int) and r.output_tokens >= 0
    assert 0.0 <= r.first_pass_consistency_rate <= 1.0
    assert 0.0 <= r.client_cache_hit_rate <= 1.0
    assert 0.0 <= r.provider_cache_hit_token_share <= 1.0  # 0 offline (no real provider)
    assert r.escalations >= 0 and r.repairs >= 0


def test_cache_on_yields_client_hits_on_repeated_intents():
    # The fixed set contains exact duplicates and paraphrases -> L1/L2 must fire when ON.
    r = run_benchmark(config=ON)
    assert r.client_cache_hit_rate > 0.0


def test_cache_off_has_no_client_hits():
    r = run_benchmark(config=OFF)
    assert r.client_cache_hit_rate == 0.0


def test_on_beats_off_on_cost_without_hurting_consistency():
    before = run_benchmark(config=OFF)
    after = run_benchmark(config=ON)
    assert after.total_cost_usd < before.total_cost_usd
    assert after.first_pass_consistency_rate >= before.first_pass_consistency_rate

    diff = compare(before, after)
    assert diff["total_cost_usd"]["reduction_pct"] > 0.0
    assert diff["total_cost_usd"]["before"] == before.total_cost_usd


def test_cascade_escalates_on_hard_intents():
    # OFF (static router) never escalates; ON (cascade) escalates on the lore-breaking intents.
    assert run_benchmark(config=OFF).escalations == 0
    assert run_benchmark(config=ON).escalations > 0
