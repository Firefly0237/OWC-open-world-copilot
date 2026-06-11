"""Offline A/B benchmark runner and report rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .benchmark import BenchmarkConfig, BenchmarkResult, compare, render_comparison, run_benchmark


@dataclass(frozen=True)
class ABConfig:
    name: str = "offline_ab"
    use_real_model: bool = False


@dataclass
class ABReport:
    name: str
    baseline: BenchmarkResult
    candidate: BenchmarkResult
    delta: dict[str, Any]
    is_real_provider: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_real_provider": self.is_real_provider,
            "baseline": self.baseline.as_dict(),
            "candidate": self.candidate.as_dict(),
            "delta": self.delta,
            "note": (
                "real-provider offline benchmark, not production online A/B"
                if self.is_real_provider
                else "offline deterministic A/B simulation, not production online A/B"
            ),
        }


def run_ab_benchmark(config: ABConfig | None = None) -> ABReport:
    """Run baseline vs optimized with the same workload and return comparable metrics."""
    config = config or ABConfig()
    baseline = BenchmarkConfig(
        f"{config.name}: baseline",
        cache="off",
        router="static",
        prefix_mode="retrieval",
        use_real_model=config.use_real_model,
    )
    candidate = BenchmarkConfig(
        f"{config.name}: optimized",
        cache="exact+semantic",
        router="cascade",
        prefix_mode="retrieval",
        use_real_model=config.use_real_model,
    )
    before = run_benchmark(config=baseline)
    after = run_benchmark(config=candidate)
    return ABReport(
        name=config.name,
        baseline=before,
        candidate=after,
        delta=compare(before, after),
        is_real_provider=config.use_real_model,
    )


def render_ab_markdown(report: ABReport) -> str:
    data = report.as_dict()
    note = data["note"]
    lines = [
        f"# owcopilot A/B benchmark: {report.name}",
        "",
        f"> {note}.",
        "",
        "## Summary",
        "",
        render_comparison(report.baseline, report.candidate),
        "",
        "## Raw Metrics",
        "",
        "```json",
        json.dumps(data, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_ab_report(report: ABReport, out_dir: str | Path = "reports") -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "ab_benchmark_latest.md"
    json_path = out / "ab_benchmark_latest.json"
    md_path.write_text(render_ab_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return md_path, json_path
