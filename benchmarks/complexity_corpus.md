# Structural-complexity benchmark

- Generator: one deterministic `random.Random(seed=20260711)` per tier (byte-level reproducible: same seed+tier => identical corpus).
- Tiers = scale factors (LDBC Datagen model); DAG shape from daggen (arXiv:1902.05808); stdlib `random` for a cross-version-stable stream.
- Deterministic, `$0` (no model / no network). The production audit, simulator, bounded explorer, and simulator witness-replay paths are called directly; no benchmark-only transition logic is used. Bands sourced in `benchmarks/evidence/narrative-complexity/complexity-profiles.json` (N2a).
- Reproduce: `python scripts/run_complexity_benchmark.py --seed 20260711`

| tier | audit open errors | quests simulated | states explored | outcomes reachable/total | dead branches | unreached branches | truncated quests | witnesses replay | content hash |
|---|--:|--:|--:|--:|--:|--:|--:|:--:|---|
| support(支撑级) | 0 | 3/3 | 65 | 29/29 | 41 | 0 | 0 | yes | `f3e90a1ab879ead4` |
| chapter(章节级) | 0 | 9/9 | 153 | 121/189 | 122 | 0 | 0 | yes | `15c004263c87dde1` |
| multiline(多线级) | 0 | 116/116 | 1725 | 2508/4206 | 1872 | 0 | 0 | yes | `0521fa68a5204392` |

All tiers audit to **zero open errors** and every quest **simulates to completion** — the generator is a clean-world oracle (the false-positive floor a benchmark needs), not a seeded-error fixture.

## support(支撑级)

- corpus: 3 quests · 65 stages · 202 branches · 95 dialogue nodes
- bounded exploration: 65 states · 29 reachable / 0 unreachable outcomes · 41 dead / 0 unreached branches · 0/3 quests truncated · witness replay passed

| parameter | profile band | achieved | in band |
|---|---|--:|:--:|
| quest_count | [1, 3] | 3 | yes |
| stages_per_quest | [15.0, 30.0] | 21.7 | yes |
| branch_factor | [3.00, 4.00] | 3.26 | yes |
| convergence_rate | [0.800, 0.950] | 0.871 | yes |
| logic_var_count | [21, 27] | 24 | yes |
| cross_quest_ref_density | [0.00, 0.50] | 0.33 | yes |
| dialogue_tree_depth | [6.0, 15.0] | 11.0 | yes |
| dialogue_tree_width | [2.00, 4.00] | 3.33 | yes |
| conditional_choice_ratio | [0.150, 0.200] | 0.156 | yes |
| logic_var_ratio.bool | [0.75, 0.85] | 0.792 | yes |
| logic_var_ratio.int | [0.05, 0.15] | 0.125 | yes |
| logic_var_ratio.enum | [0.05, 0.15] | 0.083 | yes |

## chapter(章节级)

- corpus: 9 quests · 153 stages · 436 branches · 217 dialogue nodes
- bounded exploration: 153 states · 121 reachable / 68 unreachable outcomes · 122 dead / 0 unreached branches · 0/9 quests truncated · witness replay passed

| parameter | profile band | achieved | in band |
|---|---|--:|:--:|
| quest_count | [3, 12] | 9 | yes |
| stages_per_quest | [8.0, 25.0] | 17.0 | yes |
| branch_factor | [2.00, 4.00] | 3.03 | yes |
| convergence_rate | [0.400, 0.700] | 0.587 | yes |
| logic_var_count | [20, 80] | 67 | yes |
| cross_quest_ref_density | [0.50, 3.00] | 1.78 | yes |
| dialogue_tree_depth | [5.0, 12.0] | 7.3 | yes |
| dialogue_tree_width | [2.00, 4.00] | 3.44 | yes |
| conditional_choice_ratio | [0.200, 0.400] | 0.295 | yes |
| logic_var_ratio.bool | [0.55, 0.75] | 0.657 | yes |
| logic_var_ratio.int | [0.10, 0.25] | 0.194 | yes |
| logic_var_ratio.enum | [0.10, 0.25] | 0.149 | yes |

## multiline(多线级)

- corpus: 116 quests · 1725 stages · 6086 branches · 7319 dialogue nodes
- bounded exploration: 1725 states · 2508 reachable / 1698 unreachable outcomes · 1872 dead / 0 unreached branches · 0/116 quests truncated · witness replay passed

| parameter | profile band | achieved | in band |
|---|---|--:|:--:|
| quest_count | [30, 150] | 116 | yes |
| stages_per_quest | [6.0, 25.0] | 14.9 | yes |
| branch_factor | [2.00, 5.00] | 3.78 | yes |
| convergence_rate | [0.100, 0.400] | 0.328 | yes |
| logic_var_count | [100, 2000] | 992 | yes |
| cross_quest_ref_density | [2.00, 10.00] | 5.26 | yes |
| dialogue_tree_depth | [8.0, 25.0] | 16.9 | yes |
| dialogue_tree_width | [2.00, 6.00] | 3.87 | yes |
| conditional_choice_ratio | [0.300, 0.600] | 0.334 | yes |
| logic_var_ratio.bool | [0.45, 0.65] | 0.562 | yes |
| logic_var_ratio.int | [0.15, 0.30] | 0.261 | yes |
| logic_var_ratio.enum | [0.15, 0.30] | 0.176 | yes |

## Reading it

- The three tiers are three narrative **shapes**, not one shape scaled up: `support` is a deeply-branched single story (few quests, high convergence), `multiline` is a wide multi-protagonist web (many quests, low convergence, dense cross-references).
- `branch_factor` and `convergence_rate` are drawn from the jointly-feasible sub-box of their bands: a quest with *n* reachable stages needs *n-1* merge edges, so the merge share is bounded below by ~1/`branch_factor` (the daggen density reality). Both achieved values still land inside their profile bands.
- `logic_var_count` is read as a bundle total (its multiline derivation reasons about whole-game state) and distributed across quests; every other parameter reads exactly as the N2a mapping table specifies.
- Exploration counters are deterministic coverage evidence, not latency measurements. `truncated quests` exposes any bound hit instead of presenting a partial search as exhaustive; `witnesses replay` executes every reachable outcome witness through the unmodified simulator as the explorer/simulator divergence guard.
