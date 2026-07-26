"""WS-E · N3 exploration benchmark over the N2 complexity corpus (seed 20260711).

Asserts only DETERMINISTIC fields (states explored, outcome coverage, dead-branch counts, the
witness-replay guard) — never wall-time, which the benchmark row deliberately does not record. The
witness-replay guard (`witnesses_replay`) is the divergence proof carried across the whole corpus:
every reachable outcome of every generated quest replays through the unmodified simulator.
"""

from __future__ import annotations

import pytest

from owcopilot.evaluation.complexity_corpus import (
    ExplorationBenchmarkRow,
    load_complexity_profiles,
    run_exploration_tier,
)

_SEED = 20260711
_TIERS = ("support", "chapter", "multiline")


@pytest.mark.parametrize("tier", _TIERS)
def test_exploration_tier_is_deterministic_and_replayable(tier: str) -> None:
    profile = load_complexity_profiles()[tier]

    first = run_exploration_tier(profile, _SEED)
    second = run_exploration_tier(profile, _SEED)

    # Deterministic: the whole row is a pure function of (profile, seed) — no wall-time field
    # exists, so byte-stable equality across two runs proves states / outcome-coverage / dead-branch
    # counts are all stable.
    assert first.model_dump() == second.model_dump()

    # The corpus-wide anti-divergence guard: every reachable witness replays through the simulator.
    assert first.witnesses_replay is True
    assert first.quest_count > 0
    assert first.total_states_explored > 0
    assert isinstance(first, ExplorationBenchmarkRow)


def test_exploration_row_records_no_walltime_field() -> None:
    # Guard the "deterministic fields only" contract at the schema level: a timing field would make
    # the row non-reproducible, so assert none exists.
    fields = set(ExplorationBenchmarkRow.model_fields)
    assert not (fields & {"elapsed", "elapsed_ms", "duration", "wall_time", "seconds"})
