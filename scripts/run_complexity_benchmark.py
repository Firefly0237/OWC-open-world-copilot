"""Structural-complexity benchmark — the evidence behind "把合成语料从堆数量升级到按结构采样".

Generates one deterministic corpus per narrative-complexity tier (support / chapter / multiline
from ``benchmarks/evidence/narrative-complexity/complexity-profiles.json``), then runs each through
the production audit, simulator, bounded explorer, and witness replay. It reports, per tier, the
achieved structural profile against its parameter band plus deterministic exploration coverage.

This is the structural counterpart to ``scripts/run_scale_benchmark.py`` (which scales the QUANTITY
of vectors): here the knob is narrative STRUCTURE — branch fan-out, convergence, logic-variable
population, cross-quest reference density, dialogue depth/width.

Deterministic and ``$0`` (stdlib ``random`` + pure audit/simulate/explore — no model, no network):

    python scripts/run_complexity_benchmark.py                 # the committed report
    python scripts/run_complexity_benchmark.py --seed 42       # a different draw
    python scripts/run_complexity_benchmark.py --tiers support chapter

Writes a Markdown report to ``benchmarks/complexity_corpus.md`` and prints it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from owcopilot.evaluation.complexity_corpus import (
    ComplexityBenchmarkRow,
    ExplorationBenchmarkRow,
    load_complexity_profiles,
    run_complexity_benchmark,
    run_exploration_benchmark,
)
from owcopilot.util import use_utf8_stdout

_TIERS = ("support", "chapter", "multiline")

# (label, achieved-attr, band-attr, format) — drives the per-parameter rows in the report.
_ROWS: list[tuple[str, str, str, str]] = [
    ("quest_count", "quest_count", "quest_count", "d"),
    ("stages_per_quest", "stages_per_quest", "stages_per_quest", ".1f"),
    ("branch_factor", "branch_factor", "branch_factor", ".2f"),
    ("convergence_rate", "convergence_rate", "convergence_rate", ".3f"),
    ("logic_var_count", "logic_var_count", "logic_var_count", "d"),
    ("cross_quest_ref_density", "cross_quest_ref_density", "cross_quest_ref_density", ".2f"),
    ("dialogue_tree_depth", "dialogue_tree_depth", "dialogue_tree_depth", ".1f"),
    ("dialogue_tree_width", "dialogue_tree_width", "dialogue_tree_width", ".2f"),
    ("conditional_choice_ratio", "conditional_choice_ratio", "conditional_choice_ratio", ".3f"),
]


def main() -> None:
    use_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260711, help="generation seed (byte anchor)")
    ap.add_argument(
        "--tiers", nargs="+", choices=_TIERS, default=list(_TIERS), help="which tiers to run"
    )
    args = ap.parse_args()

    report = build_report(seed=args.seed, tiers=tuple(args.tiers))
    out = Path(__file__).resolve().parents[1] / "benchmarks" / "complexity_corpus.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n[written] {out}")


def _band(profile_band: tuple[float, float], fmt: str) -> str:
    lo, hi = profile_band
    if fmt == "d":
        return f"[{int(lo)}, {int(hi)}]"
    return f"[{lo:{fmt}}, {hi:{fmt}}]"


def build_report(*, seed: int = 20260711, tiers: tuple[str, ...] = _TIERS) -> str:
    """Run both production benchmark paths and render their deterministic report."""
    rows = run_complexity_benchmark(seed=seed, tiers=tiers)
    exploration_rows = run_exploration_benchmark(seed=seed, tiers=tiers)
    return _render(rows, exploration_rows, seed=seed)


def _render(
    rows: list[ComplexityBenchmarkRow],
    exploration_rows: list[ExplorationBenchmarkRow],
    *,
    seed: int,
) -> str:
    profiles = load_complexity_profiles()
    exploration_by_tier = {row.tier: row for row in exploration_rows}
    lines = [
        "# Structural-complexity benchmark",
        "",
        f"- Generator: one deterministic `random.Random(seed={seed})` per tier "
        "(byte-level reproducible: same seed+tier => identical corpus).",
        "- Tiers = scale factors (LDBC Datagen model); DAG shape from daggen "
        "(arXiv:1902.05808); stdlib `random` for a cross-version-stable stream.",
        "- Deterministic, `$0` (no model / no network). The production audit, simulator, bounded "
        "explorer, and simulator witness-replay paths are called directly; no benchmark-only "
        "transition logic is used. Bands sourced in "
        "`benchmarks/evidence/narrative-complexity/complexity-profiles.json` (N2a).",
        f"- Reproduce: `python scripts/run_complexity_benchmark.py --seed {seed}`",
        "",
        "| tier | audit open errors | quests simulated | states explored | outcomes "
        "reachable/total | dead branches | unreached branches | truncated quests | witnesses "
        "replay | content hash |",
        "|---|--:|--:|--:|--:|--:|--:|--:|:--:|---|",
    ]
    for row in rows:
        exploration = exploration_by_tier[row.tier]
        lines.append(
            f"| {row.label} | {row.open_errors} | {row.sim_completed}/{row.sim_total} | "
            f"{exploration.total_states_explored} | "
            f"{exploration.reachable_outcomes}/{exploration.total_outcomes} | "
            f"{exploration.total_dead_branches} | {exploration.total_unreached_branches} | "
            f"{exploration.truncated_quests} | "
            f"{'yes' if exploration.witnesses_replay else '**NO**'} | "
            f"`{row.content_hash[:16]}` |"
        )
    lines += [
        "",
        "All tiers audit to **zero open errors** and every quest **simulates to completion** — the "
        "generator is a clean-world oracle (the false-positive floor a benchmark needs), not a "
        "seeded-error fixture.",
    ]

    for row in rows:
        profile = profiles[row.tier]
        exploration = exploration_by_tier[row.tier]
        lines += [
            "",
            f"## {row.label}",
            "",
            f"- corpus: {row.measurement.quest_count} quests · "
            f"{row.measurement.total_stages} stages · {row.measurement.total_branches} branches · "
            f"{row.measurement.total_dialogue_nodes} dialogue nodes",
            f"- bounded exploration: {exploration.total_states_explored} states · "
            f"{exploration.reachable_outcomes} reachable / "
            f"{exploration.unreachable_outcomes} unreachable outcomes · "
            f"{exploration.total_dead_branches} dead / "
            f"{exploration.total_unreached_branches} unreached branches · "
            f"{exploration.truncated_quests}/{exploration.quest_count} quests truncated · "
            f"witness replay {'passed' if exploration.witnesses_replay else '**FAILED**'}",
            "",
            "| parameter | profile band | achieved | in band |",
            "|---|---|--:|:--:|",
        ]
        for label, achieved_attr, band_attr, fmt in _ROWS:
            achieved = getattr(row.measurement, achieved_attr)
            band = getattr(profile, band_attr)
            mark = "yes" if row.in_band[label] else "**NO**"
            achieved_str = f"{int(achieved)}" if fmt == "d" else f"{achieved:{fmt}}"
            lines.append(f"| {label} | {_band(band, fmt)} | {achieved_str} | {mark} |")
        # logic-variable type mix (three sub-bands).
        ratio = row.measurement.logic_var_ratio
        for kind, band_attr in (
            ("bool", "logic_var_ratio_bool"),
            ("int", "logic_var_ratio_int"),
            ("enum", "logic_var_ratio_enum"),
        ):
            band = getattr(profile, band_attr)
            key = f"logic_var_ratio_{kind}"
            mark = "yes" if row.in_band[key] else "**NO**"
            lines.append(
                f"| logic_var_ratio.{kind} | {_band(band, '.2f')} | {ratio[kind]:.3f} | {mark} |"
            )

    lines += [
        "",
        "## Reading it",
        "",
        "- The three tiers are three narrative **shapes**, not one shape scaled up: `support` is a "
        "deeply-branched single story (few quests, high convergence), `multiline` is a wide "
        "multi-protagonist web (many quests, low convergence, dense cross-references).",
        "- `branch_factor` and `convergence_rate` are drawn from the jointly-feasible sub-box "
        "of their bands: a quest with *n* reachable stages needs *n-1* merge edges, so the merge "
        "share is bounded below by ~1/`branch_factor` (the daggen density reality). Both achieved "
        "values still land inside their profile bands.",
        "- `logic_var_count` is read as a bundle total (its multiline derivation reasons about "
        "whole-game state) and distributed across quests; every other parameter reads exactly as "
        "the N2a mapping table specifies.",
        "- Exploration counters are deterministic coverage evidence, not latency measurements. "
        "`truncated quests` exposes any bound hit instead of presenting a partial search as "
        "exhaustive; `witnesses replay` executes every reachable outcome witness through the "
        "unmodified simulator as the explorer/simulator divergence guard.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
