"""allowed_uses is a retrieval filter, not a decorative label (B3 / N2.1).

A reference is only recalled for a use its uploader permitted: genesis and expansion ask for
``inspiration``, the draft chains will ask for ``drafting``, and library browsing asks for
nothing in particular and still sees the whole shelf.
"""

from __future__ import annotations

import pytest

from owcopilot.app.actions import (
    add_reference_action,
    run_world_expand_action,
    run_world_seed_action,
)
from owcopilot.content.models import POI, ContentBundle, Entity, EntityType, RegionBrief
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.models import USE_DRAFTING, USE_INSPIRATION
from owcopilot.pipeline.project import ProjectContext

_QUERY = "商会 盐道"
_INSPIRATION_ONLY = "灵感稿"
_DRAFTING_ONLY = "草稿稿"


def _seed(tmp_path) -> ContentStore:
    """Two references that answer the same query, permitted for different uses."""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(
        root,
        title=_INSPIRATION_ONLY,
        text="潮汐沉城的商会为盐道控制权明争暗斗，谁掌握盐，谁就掌握王座。",
        allowed_uses=[USE_INSPIRATION],
    )
    add_reference_action(
        root,
        title=_DRAFTING_ONLY,
        text="枯井镇的商会同样围绕盐道彼此设局，账册与船期都是筹码。",
        allowed_uses=[USE_DRAFTING],
    )
    return root


def _titles(project: ProjectContext, *, use: str | None) -> set[str]:
    pack = project.reference_context_builder.build(_QUERY, use=use, limit=8)
    return {str(hit.metadata.get("source_title")) for hit in pack.hits}


def test_retrieval_recalls_only_sources_that_allow_the_use(tmp_path) -> None:
    root = _seed(tmp_path)
    project = ProjectContext.open(root, sqlite_path=tmp_path / "h.sqlite")
    try:
        assert _titles(project, use=None) == {_INSPIRATION_ONLY, _DRAFTING_ONLY}
        assert _titles(project, use=USE_INSPIRATION) == {_INSPIRATION_ONLY}
        assert _titles(project, use=USE_DRAFTING) == {_DRAFTING_ONLY}
    finally:
        project.close()


def test_disallowed_chunk_cannot_be_materialised_by_ref(tmp_path) -> None:
    """The materialisation query is the leg-independent gate: the semantic leg ranks refs without
    ever seeing allowed_uses, so a ref it surfaces must still fail to resolve to a row."""
    root = _seed(tmp_path)
    project = ProjectContext.open(root, sqlite_path=tmp_path / "h.sqlite")
    try:
        store = project.sqlite_store
        drafting_refs = [
            str(hit["ref"])
            for hit in store.search_reference_chunks(_QUERY, limit=8, use=USE_DRAFTING)
        ]
        assert drafting_refs

        assert store.reference_chunks_by_refs(drafting_refs, use=USE_DRAFTING).keys() == set(
            drafting_refs
        )
        assert store.reference_chunks_by_refs(drafting_refs, use=USE_INSPIRATION) == {}
    finally:
        project.close()


def test_unknown_use_is_refused_with_guidance(tmp_path) -> None:
    root = _seed(tmp_path)
    project = ProjectContext.open(root, sqlite_path=tmp_path / "h.sqlite")
    try:
        with pytest.raises(ValueError) as excinfo:
            project.reference_context_builder.build(_QUERY, use="灵感")
        message = str(excinfo.value)
        assert "未知的素材用途" in message
        assert USE_INSPIRATION in message and USE_DRAFTING in message
    finally:
        project.close()


def test_world_seed_only_grounds_on_references_allowed_for_inspiration(tmp_path) -> None:
    """End to end through the offline genesis run: the same text grounds a world when the uploader
    allowed it for inspiration, and does not when they only allowed drafting."""
    brief = {
        "idea": "A steam-powered border world where forests remember old wars.",
        "reference_query": "three factions failing resource frontier",
        "faction_count": 2,
        "quest_count": 2,
    }
    text = "Three factions fight over a failing resource on a remembered frontier."

    drafting_root = tmp_path / "drafting"
    ContentStore(drafting_root).save(ContentBundle())
    add_reference_action(
        drafting_root, title="Frontier structure", text=text, allowed_uses=[USE_DRAFTING]
    )
    assert run_world_seed_action(drafting_root, brief=brief)["inspiration_context_refs"] == []

    inspiration_root = tmp_path / "inspiration"
    ContentStore(inspiration_root).save(ContentBundle())
    add_reference_action(
        inspiration_root, title="Frontier structure", text=text, allowed_uses=[USE_INSPIRATION]
    )
    assert run_world_seed_action(inspiration_root, brief=brief)["inspiration_context_refs"]


def _seed_expand_world(root) -> None:
    ContentStore(root).save(
        ContentBundle(
            regions={"region_crown": RegionBrief(id="region_crown", name="冠城")},
            entities={
                "fac_charter": Entity(id="fac_charter", name="炉心公约", type=EntityType.FACTION)
            },
            pois={
                "loc_gate": POI(
                    id="loc_gate",
                    name="冠门车站",
                    region_id="region_crown",
                    controlling_faction="fac_charter",
                )
            },
        )
    )


def test_world_expand_only_grounds_on_references_allowed_for_inspiration(tmp_path) -> None:
    """Same contract as genesis (docstring above): expansion's inspiration_pack must also honour
    allowed_uses, not just genesis's — regression coverage for the gap where expand.py's retrieval
    call had no ``use=`` at all (every source was recalled regardless of its declared uses)."""
    brief = {
        "focus_ref": "region:region_crown",
        "reference_query": "three factions failing resource frontier",
        "poi_count": 1,
        "npc_count": 0,
        "quest_count": 0,
    }
    text = "Three factions fight over a failing resource on a remembered frontier."

    drafting_root = tmp_path / "expand_drafting"
    _seed_expand_world(drafting_root)
    add_reference_action(
        drafting_root, title="Frontier structure", text=text, allowed_uses=[USE_DRAFTING]
    )
    assert run_world_expand_action(drafting_root, brief=brief)["inspiration_context_refs"] == []

    inspiration_root = tmp_path / "expand_inspiration"
    _seed_expand_world(inspiration_root)
    add_reference_action(
        inspiration_root, title="Frontier structure", text=text, allowed_uses=[USE_INSPIRATION]
    )
    assert run_world_expand_action(inspiration_root, brief=brief)["inspiration_context_refs"]
