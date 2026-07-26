"""N2b — the profile-parameterized synthetic structural-complexity generator.

Guards the four properties that make it a benchmark generator rather than a random content dump:
byte-level reproducibility (LDBC determinism), audit-cleanliness + full simulatability (a
clean-world oracle), and achieved-within-band structure (the corpus actually mimics the N2a
profiles). Tests only assemble inputs and assert on the production functions — generation,
measurement, band evaluation, audit and simulation are all reused, never re-implemented.
"""

from __future__ import annotations

import pytest

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.hash import content_hash
from owcopilot.evaluation.complexity_corpus import (
    evaluate_complexity_bands,
    generate_complexity_bundle,
    load_complexity_profiles,
    measure_complexity,
    run_complexity_benchmark,
)
from owcopilot.logic.simulate import simulate_quest

_TIERS = ("support", "chapter", "multiline")


def _profiles():
    return load_complexity_profiles()


def test_profiles_load_three_tiers_with_bands() -> None:
    profiles = _profiles()
    assert set(profiles) == set(_TIERS)
    for tier in _TIERS:
        profile = profiles[tier]
        assert profile.tier == tier
        # every band is an ascending [lo, hi] pair.
        for band in (
            profile.quest_count,
            profile.stages_per_quest,
            profile.branch_factor,
            profile.convergence_rate,
            profile.logic_var_count,
            profile.logic_var_ratio_bool,
            profile.logic_var_ratio_int,
            profile.logic_var_ratio_enum,
            profile.cross_quest_ref_density,
            profile.dialogue_tree_depth,
            profile.dialogue_tree_width,
            profile.conditional_choice_ratio,
        ):
            assert len(band) == 2 and band[0] <= band[1], (tier, band)


@pytest.mark.parametrize("tier", _TIERS)
def test_byte_level_reproducible(tier: str) -> None:
    """Same (profile, seed) => byte-identical corpus — the LDBC determinism guarantee, proven at the
    serialized-bytes level (content hash + full JSON), not just object equality."""
    profile = _profiles()[tier]
    first = generate_complexity_bundle(profile, seed=7)
    second = generate_complexity_bundle(profile, seed=7)
    assert content_hash(first) == content_hash(second)
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("tier", _TIERS)
def test_distinct_seeds_give_distinct_corpora(tier: str) -> None:
    """Determinism is not a constant: different seeds produce different corpora (the generator is
    genuinely sampling the profile, not emitting one fixed artifact)."""
    profile = _profiles()[tier]
    hashes = {content_hash(generate_complexity_bundle(profile, seed=s)) for s in range(4)}
    assert len(hashes) == 4


@pytest.mark.parametrize("tier", _TIERS)
def test_generated_corpus_audits_clean(tier: str) -> None:
    """Zero open ERROR issues under the full default rule registry — the false-positive floor a
    benchmark oracle must hold (mirrors the acceptance world's clean-world gate)."""
    profile = _profiles()[tier]
    bundle = generate_complexity_bundle(profile, seed=3)
    runner = AuditRunner(build_default_rule_registry())
    result = runner.run(AuditContext.from_bundle(bundle))
    assert result.open_errors == []


@pytest.mark.parametrize("tier", _TIERS)
def test_every_quest_simulates_to_completion(tier: str) -> None:
    """Each generated quest's forward stage-DAG walks to ``completed`` under the pure simulator (no
    deadlock / blocked / cycle) — the graph is playable, not just well-formed."""
    profile = _profiles()[tier]
    bundle = generate_complexity_bundle(profile, seed=3)
    assert bundle.quests
    for quest in bundle.quests.values():
        assert simulate_quest(quest).status == "completed", quest.id


@pytest.mark.parametrize("tier", _TIERS)
def test_achieved_structure_within_profile_bands(tier: str) -> None:
    """Across several seeds every achieved structural parameter lands inside its evidence-derived
    band — the corpus mimics the profile, which is the whole point of N2b over a quantity dump."""
    profile = _profiles()[tier]
    for seed in range(8):
        measurement = measure_complexity(generate_complexity_bundle(profile, seed))
        bands = evaluate_complexity_bands(measurement, profile)
        failed = [name for name, ok in bands.items() if not ok]
        assert not failed, (tier, seed, failed, measurement.model_dump())


def test_logic_var_count_is_bundle_total() -> None:
    """``logic_var_count`` is measured as the bundle-wide variable population (its multiline
    derivation reasons about whole-game state), i.e. the sum over quests."""
    profile = _profiles()["chapter"]
    bundle = generate_complexity_bundle(profile, seed=1)
    total = sum(len(q.logic.variables) for q in bundle.quests.values() if q.logic)
    assert measure_complexity(bundle).logic_var_count == total
    assert profile.logic_var_count[0] <= total <= profile.logic_var_count[1]


def test_benchmark_all_tiers_clean_simulated_and_in_band() -> None:
    """The fixed benchmark: every tier audits clean, fully simulates, and is entirely in-band — the
    integration guard mirroring ``run_repair_benchmark``'s invariant test."""
    rows = run_complexity_benchmark()
    assert [row.tier for row in rows] == list(_TIERS)
    for row in rows:
        assert row.audit_clean, (row.tier, row.open_errors)
        assert row.sim_all_completed, (row.tier, row.sim_completed, row.sim_total)
        assert row.all_in_band, (row.tier, {k: v for k, v in row.in_band.items() if not v})
        assert row.sim_total > 0
