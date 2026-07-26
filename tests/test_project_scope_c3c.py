"""Scale-P0 G2-C C3c: ProjectContext.open loads the current scope's *effective* (version-resolved)
bundle, so the graph, audit and impact analysis run on that version's content -- the audit/impact
reduce-N that C2 deferred. The default scope has no overlay, so it stays byte-identical.
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.project import ProjectContext


def _npc(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _seed_baseline(root) -> ContentStore:
    store = ContentStore(root)
    bundle = ContentBundle()
    bundle.entities["e1"] = _npc("e1", "Alice")
    bundle.entities["e2"] = _npc("e2", "Bob")
    store.save(bundle)
    return store


def test_open_default_scope_bundle_equals_load(tmp_path) -> None:
    """INV-1: opening at the default scope loads exactly what load() would (byte-identical)."""
    root = tmp_path / "content"
    _seed_baseline(root)
    ctx = ProjectContext.open(root)
    try:
        assert ctx.bundle.model_dump() == ContentStore(root).load().model_dump()
    finally:
        ctx.close()


def test_open_version_scope_sees_effective_bundle(tmp_path) -> None:
    root = tmp_path / "content"
    store = _seed_baseline(root)
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e1"] = _npc("e1", "Alice-v2")  # override in v2
    b2.entities["e3"] = _npc("e3", "Carol")  # add in v2
    store.save_scoped(b2, version="v2")
    ctx = ProjectContext.open(root, world_id="default", version="v2")
    try:
        # ProjectContext used load_scoped -> graph/audit/retrieval all see v2's effective content
        assert ctx.bundle.entities["e1"].name == "Alice-v2"  # override
        assert "e3" in ctx.bundle.entities  # add
        assert ctx.bundle.entities["e2"].name == "Bob"  # inherited from baseline
        # the graph is built from the scoped bundle, so the v2-only node is present downstream
        assert ctx.graph.has_node("entity:e3")
    finally:
        ctx.close()


def test_open_baseline_unaffected_by_a_derived_version(tmp_path) -> None:
    root = tmp_path / "content"
    store = _seed_baseline(root)
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e1"] = _npc("e1", "Alice-v2")
    store.save_scoped(b2, version="v2")
    ctx = ProjectContext.open(root, version="v1")  # baseline scope
    try:
        assert ctx.bundle.entities["e1"].name == "Alice"  # baseline unaffected by v2
        assert set(ctx.bundle.entities) == {"e1", "e2"}
    finally:
        ctx.close()
