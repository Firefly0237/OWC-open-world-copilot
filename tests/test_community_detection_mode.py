"""Tests for community detection algorithm selection and honest stability notes.

Covers:
- OWCOPILOT_COMMUNITY=cnm (default): CNM algorithm, stable output via post-sort
- OWCOPILOT_COMMUNITY=leiden: Leiden algorithm (opt-in, skipped if leidenalg missing)
- Community module no longer uses the word DETERMINISTIC in a misleading way
"""

from __future__ import annotations

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.graph.community import detect_communities


def _simple_bundle() -> ContentBundle:
    """A small but clustered world: two cliques loosely connected."""
    ents = {}

    def e(ref: str, name: str) -> None:
        ents[ref] = Entity(id=ref, name=name, type=EntityType.NPC, description=f"{name}角色")

    e("fac_a", "甲方")
    e("fac_b", "乙方")
    e("npc_a1", "甲一")
    e("npc_a2", "甲二")
    e("npc_b1", "乙一")
    e("npc_b2", "乙二")

    rels = [
        Relation(source="npc_a1", target="fac_a", kind="member_of"),
        Relation(source="npc_a2", target="fac_a", kind="member_of"),
        Relation(source="npc_b1", target="fac_b", kind="member_of"),
        Relation(source="npc_b2", target="fac_b", kind="member_of"),
        # sparse cross-cluster link
        Relation(source="fac_a", target="fac_b", kind="rival_of"),
    ]
    return ContentBundle(entities=ents, relations=rels)


# ---------------------------------------------------------------------------
# CNM (default)
# ---------------------------------------------------------------------------


def test_cnm_detect_produces_communities(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "cnm")
    bundle = _simple_bundle()
    communities = detect_communities(bundle)
    assert len(communities) >= 1


def test_cnm_output_is_stable_across_repeated_calls(monkeypatch) -> None:
    """The post-sort guarantee makes CNM output stable for the same graph."""
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "cnm")
    bundle = _simple_bundle()
    first = detect_communities(bundle)
    second = detect_communities(bundle)
    assert [c.id for c in first] == [c.id for c in second]
    assert [sorted(c.member_refs) for c in first] == [sorted(c.member_refs) for c in second]


def test_cnm_default_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_COMMUNITY", raising=False)
    bundle = _simple_bundle()
    communities = detect_communities(bundle)
    assert communities  # just confirm it runs without error


def test_community_module_docstring_does_not_claim_strict_determinism() -> None:
    """Guard: the module docstring must NOT make the old false DETERMINISTIC claim.

    The old (wrong) docstring said:
        'The partition here is DETERMINISTIC — greedy modularity maximisation ... no RNG'
    CNM uses Python set ordering internally and is NOT strictly deterministic.
    The corrected docstring documents the stability guarantee and the tie-breaking behaviour.
    """
    import owcopilot.graph.community as community_mod

    docstring = community_mod.__doc__ or ""
    # The old exact false claim: "is DETERMINISTIC" or "The partition here is DETERMINISTIC"
    bad_claim = "partition here is DETERMINISTIC"
    assert bad_claim not in docstring, (
        f"Module docstring should not contain {bad_claim!r}. "
        "CNM is only stable via post-sort, not strictly deterministic. "
        "Use 'stable' and document tie-breaking instead."
    )
    # The new docstring should explicitly warn against the DETERMINISTIC label
    assert "stable" in docstring.lower(), (
        "Module docstring should describe the stability guarantee (use 'stable')"
    )


def test_community_docstring_mentions_leiden_as_opt_in() -> None:
    """The docstring should mention Leiden as an alternative / opt-in option."""
    import owcopilot.graph.community as community_mod

    docstring = community_mod.__doc__ or ""
    assert "leiden" in docstring.lower() or "Leiden" in docstring, (
        "Module docstring should mention Leiden as an opt-in alternative."
    )


# ---------------------------------------------------------------------------
# Leiden (opt-in)
# ---------------------------------------------------------------------------


def test_leiden_mode_raises_helpful_import_error_when_missing(monkeypatch) -> None:
    """When OWCOPILOT_COMMUNITY=leiden and leidenalg is not installed, raises ImportError."""
    import unittest.mock

    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "leiden")

    # Simulate missing leidenalg regardless of what's actually installed
    with unittest.mock.patch.dict("sys.modules", {"leidenalg": None, "igraph": None}):
        with pytest.raises(ImportError, match="leidenalg"):
            detect_communities(_simple_bundle())


@pytest.mark.skipif(
    not (
        __import__("importlib").util.find_spec("leidenalg") is not None
        and __import__("importlib").util.find_spec("igraph") is not None
    ),
    reason="leidenalg + python-igraph not installed (opt-in only)",
)
def test_leiden_mode_produces_same_partition_given_fixed_seed(monkeypatch) -> None:
    """Leiden with a fixed seed must produce an identical partition across calls."""
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "leiden")
    bundle = _simple_bundle()
    first = detect_communities(bundle)
    second = detect_communities(bundle)
    assert [c.id for c in first] == [c.id for c in second]
    assert [sorted(c.member_refs) for c in first] == [sorted(c.member_refs) for c in second]


@pytest.mark.skipif(
    not (
        __import__("importlib").util.find_spec("leidenalg") is not None
        and __import__("importlib").util.find_spec("igraph") is not None
    ),
    reason="leidenalg + python-igraph not installed (opt-in only)",
)
def test_leiden_mode_clusters_connected_faction_and_members(monkeypatch) -> None:
    """Leiden should cluster each faction with its members (basic sanity check)."""
    monkeypatch.setenv("OWCOPILOT_COMMUNITY", "leiden")
    bundle = _simple_bundle()
    communities = detect_communities(bundle)
    assert len(communities) >= 1
    # fac_a and at least one of its members should be in the same community
    for c in communities:
        refs = set(c.member_refs)
        if "entity:fac_a" in refs:
            assert "entity:npc_a1" in refs or "entity:npc_a2" in refs, (
                "Leiden should co-cluster fac_a with its members"
            )


# ---------------------------------------------------------------------------
# Weight detection bug regression (RT5)
# ---------------------------------------------------------------------------


def test_leiden_weight_detection_uses_nx_get_edge_attributes() -> None:
    """Regression: weight detection in _leiden_partition must NOT use `"weight" in <generator>`.

    `"weight" in (g.edges[u, v] for u, v in g.edges())` is always False because Python's
    `in` operator calls `__contains__` on the generator, which is not implemented and always
    returns False after exhausting the generator — it never finds "weight".

    The fix uses ``nx.get_edge_attributes(g, "weight")`` which returns a non-empty dict
    when at least one edge carries the attribute.

    This test does NOT require leidenalg.  It validates the networkx weight-detection logic
    directly, so CI can catch a regression without the optional dependency installed.
    """
    import networkx as nx

    # Build a small weighted graph (weight > 1 on one edge)
    g = nx.Graph()
    g.add_node("a")
    g.add_node("b")
    g.add_edge("a", "b", weight=3)

    # Old (broken) approach: always False
    broken_result = "weight" in (g.edges[u, v] for u, v in g.edges())
    assert not broken_result, (
        "Confirmed: `'weight' in <generator>` is always False — this is the original bug."
    )

    # Correct approach: nx.get_edge_attributes returns non-empty dict when weights exist
    correct_result = bool(nx.get_edge_attributes(g, "weight"))
    assert correct_result, (
        "nx.get_edge_attributes(g, 'weight') must return a non-empty dict for a weighted graph."
    )

    # Sanity: an unweighted graph returns empty dict (no false positives)
    g_unweighted = nx.Graph()
    g_unweighted.add_node("x")
    g_unweighted.add_node("y")
    g_unweighted.add_edge("x", "y")
    assert not nx.get_edge_attributes(g_unweighted, "weight"), (
        "nx.get_edge_attributes must return empty dict for graph with no weight attribute."
    )


def test_community_module_uses_nx_get_edge_attributes_not_generator_in() -> None:
    """Source-level check: _leiden_partition must not contain the broken generator pattern."""
    import inspect

    from owcopilot.graph import community as community_mod

    source = inspect.getsource(community_mod._leiden_partition)

    # The broken pattern: "weight" in (... for ...)  — generator membership test
    assert '"weight" in (g.edges' not in source, (
        "_leiden_partition still contains the broken generator membership test "
        '(`"weight" in (g.edges[u, v] for ...)`). '
        "Replace with `nx.get_edge_attributes(g, 'weight')` or equivalent."
    )
    # The correct pattern should be present
    assert "get_edge_attributes" in source, (
        "_leiden_partition should use nx.get_edge_attributes to detect edge weights."
    )
