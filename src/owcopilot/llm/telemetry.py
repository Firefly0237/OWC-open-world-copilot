"""Per-call cost/usage accounting. The gateway records one CallRecord per model call.

Two different "caches" surface here — keep them straight (this is the core P2 distinction):
  * CLIENT cache (our L1 ExactCache / L2 SemanticCache): a hit means we *never called the
    provider at all*. Recorded as `cache_hit=True` with zero tokens -> zero cost.
  * PROVIDER cache (DeepSeek server-side prefix cache): the call still happens, but the input
    tokens whose prefix matched are billed at the much cheaper "cache-hit" rate. Recorded as
    `cached_input_tokens` (a subset of `input_tokens`, i.e. prompt_cache_hit_tokens).

Prices are per *tier* and ILLUSTRATIVE (USD per 1M tokens). The cache-hit input price is far
below the miss price (DeepSeek: hit ~= 1/50 of miss on the flash tier), which is exactly why a
repeated long lore prefix is so much cheaper the second time. The official provider pricing
page is authoritative — do not hard-code long-term prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# tier -> (cache_hit_input, cache_miss_input, output)  USD per 1M tokens. ILLUSTRATIVE.
PRICES: dict[str, tuple[float, float, float]] = {
    "cheap": (0.006, 0.30, 1.20),  # e.g. deepseek-v4-flash class (hit ~= 1/50 of miss)
    "frontier": (0.10, 5.00, 25.00),  # e.g. deepseek-v4-pro / Opus / GPT-4 class
    "mock": (0.00, 0.00, 0.00),
}


@dataclass
class CallRecord:
    task: str
    tier: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0  # provider/server-side prefix-cache hit tokens (subset of input)
    cache_hit: bool = False  # CLIENT-side (L1/L2) short-circuit: no provider call happened
    latency_ms: float = 0.0

    @property
    def cost_usd(self) -> float:
        hit_price, miss_price, out_price = PRICES.get(self.tier, (0.0, 0.0, 0.0))
        hit = min(max(self.cached_input_tokens, 0), self.input_tokens)
        miss = self.input_tokens - hit
        return (hit * hit_price + miss * miss_price + self.output_tokens * out_price) / 1_000_000


@dataclass
class TelemetryCollector:
    records: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    @property
    def cache_hit_rate(self) -> float:
        """CLIENT-side hit rate: fraction of calls short-circuited by L1/L2 (zero-cost)."""
        return (
            (sum(1 for r in self.records if r.cache_hit) / len(self.records))
            if self.records
            else 0.0
        )

    @property
    def provider_cache_hit_tokens(self) -> int:
        return sum(r.cached_input_tokens for r in self.records)

    @property
    def provider_cache_hit_token_share(self) -> float:
        """Server-side prefix-cache coverage = Σ hit input tokens / Σ input tokens.
        Non-zero only against a real provider that reports prompt_cache_hit_tokens."""
        total_in = self.total_input_tokens
        return (self.provider_cache_hit_tokens / total_in) if total_in else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return (
            (sum(r.latency_ms for r in self.records) / len(self.records)) if self.records else 0.0
        )

    def summary(self) -> dict:
        return {
            "calls": len(self.records),
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 3),
            "provider_cache_hit_token_share": round(self.provider_cache_hit_token_share, 3),
            "mean_latency_ms": round(self.mean_latency_ms, 3),
            "total_cost_usd": round(self.total_cost, 6),
        }

    def render_table(self) -> str:
        head = (
            f"{'task':<12}{'tier':<10}{'in_tok':>8}{'out_tok':>9}"
            f"{'phit_tok':>9}{'chit':>6}{'cost($)':>12}"
        )
        lines = [head, "-" * len(head)]
        for r in self.records:
            lines.append(
                f"{r.task:<12}{r.tier:<10}{r.input_tokens:>8}{r.output_tokens:>9}"
                f"{r.cached_input_tokens:>9}{('Y' if r.cache_hit else '-'):>6}{r.cost_usd:>12.6f}"
            )
        lines.append("-" * len(head))
        s = self.summary()
        phit = s["provider_cache_hit_token_share"]
        lines.append(
            f"TOTAL  calls={s['calls']}  in={s['input_tokens']}  out={s['output_tokens']}  "
            f"client_hit={s['cache_hit_rate']:.0%}  provider_hit_share={phit:.0%}  "
            f"cost=${s['total_cost_usd']:.6f}"
        )
        return "\n".join(lines)
