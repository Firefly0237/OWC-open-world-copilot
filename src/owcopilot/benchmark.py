"""P2 benchmark harness — the 'ruler' for every cost optimisation.

`run_benchmark(intents, config=...)` assembles a gateway per `BenchmarkConfig` (cache on/off,
static vs cascade router, retrieval vs stable prefix, fake vs real model), runs the full
plan->generate->verify->[repair] loop over a fixed intent set on ONE shared gateway (so the
cache warms across intents), and returns a `BenchmarkResult` of comparable metrics.

`compare(before, after)` turns two results into a before/after delta — the headline P2
deliverable. Everything is offline/$0 by default; `BenchmarkConfig(use_real_model=True)`
swaps in DeepSeek (deepseek-v4-flash / deepseek-v4-pro).

Reuses the existing seam (guardrail #1): only the gateway *assembly* changes between configs;
no call site is touched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .assembly import PrefixMode, RouterMode, build_grounded_pipeline
from .examples.benchmark_intents import (
    BENCHMARK_INTENTS,
    BenchmarkIntent,
    BenchmarkProvider,
    scenarios,
)
from .llm.cache import build_cache_backend
from .llm.gateway import LLMProvider, OpenAICompatProvider
from .worldbible.models import WorldBible


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class BenchmarkConfig:
    name: str
    cache: Literal["off", "exact", "exact+semantic", "redis", "redis+semantic"] = "off"
    router: RouterMode = "static"
    prefix_mode: PrefixMode = "retrieval"
    use_real_model: bool = False
    semantic_threshold: float = 0.9


# The two headline configs for the before/after page.
OFF = BenchmarkConfig("OFF (baseline)", cache="off", router="static", prefix_mode="retrieval")
ON = BenchmarkConfig(
    "ON (optimised)", cache="exact+semantic", router="cascade", prefix_mode="retrieval"
)


# --------------------------------------------------------------------------- result
@dataclass
class BenchmarkResult:
    config_name: str
    n_intents: int
    total_cost_usd: float
    input_tokens: int
    output_tokens: int
    client_cache_hit_rate: float  # L1/L2 short-circuits / total calls
    provider_cache_hit_token_share: float  # server prefix-cache token coverage (real model only)
    mean_latency_ms: float
    first_pass_consistency_rate: float  # share clean with 0 repairs — the quality floor
    escalations: int  # cascade cheap->strong upgrades
    repairs: int  # total verify->repair attempts across the set

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- assembly
def _build_cache(config: BenchmarkConfig):
    return build_cache_backend(config.cache, semantic_threshold=config.semantic_threshold)


def _build_app(wb: WorldBible, config: BenchmarkConfig, intents: list[BenchmarkIntent]):
    from .demo import load_dotenv

    if config.use_real_model:
        load_dotenv()
        cheap: LLMProvider = OpenAICompatProvider(model="deepseek-v4-flash")
        strong: LLMProvider = OpenAICompatProvider(model="deepseek-v4-pro")
    else:
        provider = BenchmarkProvider(scenarios(intents))  # one provider; branches on tier
        cheap = strong = provider

    app, telemetry, generator = build_grounded_pipeline(
        wb,
        cheap_provider=cheap,
        frontier_provider=strong,
        use_llm_repair=True,
        router_mode=config.router,
        cache=_build_cache(config),
        prefix_mode=config.prefix_mode,
    )
    return app, telemetry, generator


# --------------------------------------------------------------------------- run
def run_benchmark(
    intents: list[BenchmarkIntent] | None = None,
    *,
    config: BenchmarkConfig = ON,
    wb: WorldBible | None = None,
) -> BenchmarkResult:
    from .demo import demo_worldbible

    intents = intents if intents is not None else BENCHMARK_INTENTS
    wb = wb if wb is not None else demo_worldbible()
    app, telemetry, generator = _build_app(wb, config, intents)

    first_pass, repairs = 0, 0
    for bi in intents:
        final = app.invoke({"intent": bi.intent, "max_repair_attempts": 2, "log": []})
        attempts = final.get("repair_attempts", 0)
        errors = [i for i in final.get("issues", []) if i.severity == "error"]
        repairs += attempts
        if attempts == 0 and not errors:
            first_pass += 1

    n = len(intents)
    return BenchmarkResult(
        config_name=config.name,
        n_intents=n,
        total_cost_usd=telemetry.total_cost,
        input_tokens=telemetry.total_input_tokens,
        output_tokens=telemetry.total_output_tokens,
        client_cache_hit_rate=telemetry.cache_hit_rate,
        provider_cache_hit_token_share=telemetry.provider_cache_hit_token_share,
        mean_latency_ms=telemetry.mean_latency_ms,
        first_pass_consistency_rate=(first_pass / n) if n else 0.0,
        escalations=getattr(generator, "escalations", 0),
        repairs=repairs,
    )


# --------------------------------------------------------------------------- compare
def _reduction_pct(before: float, after: float) -> float:
    """Percent reduction of `after` vs `before` (positive = cheaper/faster)."""
    return ((before - after) / before * 100.0) if before else 0.0


def compare(before: BenchmarkResult, after: BenchmarkResult) -> dict:
    """Before/after deltas. Cost/tokens/latency are reported as % reductions (higher = better);
    hit rates and first-pass consistency are reported as absolute values for each run."""
    return {
        "total_cost_usd": {
            "before": before.total_cost_usd,
            "after": after.total_cost_usd,
            "reduction_pct": _reduction_pct(before.total_cost_usd, after.total_cost_usd),
        },
        "input_tokens": {
            "before": before.input_tokens,
            "after": after.input_tokens,
            "reduction_pct": _reduction_pct(before.input_tokens, after.input_tokens),
        },
        "output_tokens": {
            "before": before.output_tokens,
            "after": after.output_tokens,
            "reduction_pct": _reduction_pct(before.output_tokens, after.output_tokens),
        },
        "mean_latency_ms": {
            "before": before.mean_latency_ms,
            "after": after.mean_latency_ms,
            "reduction_pct": _reduction_pct(before.mean_latency_ms, after.mean_latency_ms),
        },
        "client_cache_hit_rate": {
            "before": before.client_cache_hit_rate,
            "after": after.client_cache_hit_rate,
        },
        "provider_cache_hit_token_share": {
            "before": before.provider_cache_hit_token_share,
            "after": after.provider_cache_hit_token_share,
        },
        "first_pass_consistency_rate": {
            "before": before.first_pass_consistency_rate,
            "after": after.first_pass_consistency_rate,
        },
        "escalations": {"before": before.escalations, "after": after.escalations},
    }


# --------------------------------------------------------------------------- rendering
def render_result(r: BenchmarkResult) -> str:
    return (
        f"{r.config_name}  (n={r.n_intents})\n"
        f"  total_cost_usd                 : ${r.total_cost_usd:.6f}\n"
        f"  input_tokens / output_tokens   : {r.input_tokens} / {r.output_tokens}\n"
        f"  client_cache_hit_rate          : {r.client_cache_hit_rate:.0%}\n"
        f"  provider_cache_hit_token_share : {r.provider_cache_hit_token_share:.0%}\n"
        f"  mean_latency_ms                : {r.mean_latency_ms:.3f}\n"
        f"  first_pass_consistency_rate    : {r.first_pass_consistency_rate:.0%}\n"
        f"  escalations / repairs          : {r.escalations} / {r.repairs}"
    )


def render_comparison(before: BenchmarkResult, after: BenchmarkResult) -> str:
    c = compare(before, after)
    lines = [
        "=" * 70,
        f"BEFORE vs AFTER   ({before.config_name}  ->  {after.config_name})",
        "=" * 70,
        f"{'metric':<32}{'before':>12}{'after':>12}{'delta':>12}",
        "-" * 68,
        f"{'total_cost_usd':<32}{before.total_cost_usd:>12.6f}{after.total_cost_usd:>12.6f}"
        f"{c['total_cost_usd']['reduction_pct']:>11.1f}%",
        f"{'input_tokens':<32}{before.input_tokens:>12}{after.input_tokens:>12}"
        f"{c['input_tokens']['reduction_pct']:>11.1f}%",
        f"{'output_tokens':<32}{before.output_tokens:>12}{after.output_tokens:>12}"
        f"{c['output_tokens']['reduction_pct']:>11.1f}%",
        f"{'mean_latency_ms':<32}{before.mean_latency_ms:>12.3f}{after.mean_latency_ms:>12.3f}"
        f"{c['mean_latency_ms']['reduction_pct']:>11.1f}%",
        f"{'client_cache_hit_rate':<32}{before.client_cache_hit_rate:>11.0%}{after.client_cache_hit_rate:>12.0%}{'':>12}",
        f"{'provider_cache_hit_token_share':<32}{before.provider_cache_hit_token_share:>11.0%}"
        f"{after.provider_cache_hit_token_share:>12.0%}{'':>12}",
        f"{'first_pass_consistency_rate':<32}{before.first_pass_consistency_rate:>11.0%}"
        f"{after.first_pass_consistency_rate:>12.0%}{'':>12}",
        f"{'escalations':<32}{before.escalations:>12}{after.escalations:>12}{'':>12}",
        "-" * 68,
        f"Headline: cost -{c['total_cost_usd']['reduction_pct']:.0f}%  "
        f"with first-pass consistency {before.first_pass_consistency_rate:.0%} -> "
        f"{after.first_pass_consistency_rate:.0%} (must not drop).",
    ]
    return "\n".join(lines)
