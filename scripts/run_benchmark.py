"""P2 cost benchmark — the before/after 'ruler'.

Offline (default, $0):
    python scripts/run_benchmark.py
      -> OFF (NoOpCache + StaticRouter) vs ON (Exact+Semantic cache + CascadeRouter)
      -> plus a retrieval vs stable-prefix prompt-structure comparison.

Live against DeepSeek (manual, costs money — needs OPENAI_BASE_URL / OPENAI_API_KEY + openai):
    python scripts/run_benchmark.py --real
      -> the same OFF/ON comparison on real tokens, then a cold-vs-hot run of the stable-prefix
         config to show the server-side prefix cache warming (provider_cache_hit_token_share up,
         cost down on the 2nd pass).
"""

import sys

from owcopilot.benchmark import (
    OFF,
    ON,
    BenchmarkConfig,
    render_comparison,
    render_result,
    run_benchmark,
)
from owcopilot.util import use_utf8_stdout


def _off_on(real: bool) -> None:
    if real:
        off = BenchmarkConfig(
            "OFF (real)", cache="off", router="static", prefix_mode="retrieval", use_real_model=True
        )
        on = BenchmarkConfig(
            "ON (real)",
            cache="exact+semantic",
            router="cascade",
            prefix_mode="retrieval",
            use_real_model=True,
        )
    else:
        off, on = OFF, ON

    before = run_benchmark(config=off)
    after = run_benchmark(config=on)
    print(render_result(before), "\n")
    print(render_result(after), "\n")
    print(render_comparison(before, after))


def _prefix_ab(real: bool) -> None:
    """Retrieval (a) vs stable-prefix (b) — the token-vs-server-cache trade-off (§2.4)."""
    a = BenchmarkConfig(
        "retrieval prefix (a)",
        cache="off",
        router="static",
        prefix_mode="retrieval",
        use_real_model=real,
    )
    b = BenchmarkConfig(
        "stable prefix (b)", cache="off", router="static", prefix_mode="stable", use_real_model=real
    )
    ra = run_benchmark(config=a)
    rb = run_benchmark(config=b)
    print("\n" + "#" * 70)
    print("Prompt structure: retrieval (a) vs stable-prefix (b)")
    print("#" * 70)
    print(render_result(ra), "\n")
    print(render_result(rb))
    if not real:
        print(
            "\nNote: offline, provider_cache_hit_token_share is 0 by design — stable prefix "
            "only pays off against a real provider's server cache (use --real to see it)."
        )


def _server_cache_cold_hot() -> None:
    """Real-only: run the stable-prefix config twice; the 2nd pass should hit DeepSeek's warm
    server cache (higher provider_cache_hit_token_share, lower cost)."""
    cfg = BenchmarkConfig(
        "stable prefix (real)",
        cache="off",
        router="static",
        prefix_mode="stable",
        use_real_model=True,
    )
    cold = run_benchmark(config=cfg)
    hot = run_benchmark(config=cfg)
    print("\n" + "#" * 70)
    print("Server prefix cache: cold (1st pass) vs hot (2nd pass)")
    print("#" * 70)
    print(render_result(cold), "\n")
    print(render_result(hot))


def main() -> None:
    use_utf8_stdout()
    real = "--real" in sys.argv
    if real:
        print("!! --real: this calls DeepSeek and costs money. Ctrl-C to abort.\n")
    _off_on(real)
    _prefix_ab(real)
    if real:
        _server_cache_cold_hot()


if __name__ == "__main__":
    main()
