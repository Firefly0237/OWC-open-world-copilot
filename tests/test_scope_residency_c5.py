"""Scale-P0 G2-C C5: cold/hot residency. A ProjectContext holds only the *active* scope's content;
other versions' data sits on disk (indexed when their scope is opened), never eager-loaded.

The heavy lifting was already delivered -- C1 (disk-resident vec0 + world/version PARTITION KEY),
C2 (scope-filtered reads), C3c (active-bundle-only load), G2-B (per-scope usearch files). This test
pins the resulting property at the ProjectContext level so a future change can't regress it into
loading every version at once. Explicit *cold archival* (physically evicting old versions from the
hot DB) is a deferred P1 optimisation -- rows are already disk-resident, not in RAM.
"""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.project import ProjectContext


def _npc(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _seed_two_versions(root) -> None:
    store = ContentStore(root)
    baseline = ContentBundle()
    baseline.entities["e1"] = _npc("e1", "Alice")
    store.save(baseline)
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["e2"] = _npc("e2", "OnlyInV2")  # content unique to v2
    store.save_scoped(b2, version="v2")


def test_context_holds_only_the_active_scope(tmp_path) -> None:
    root = tmp_path / "content"
    db = str(tmp_path / "runtime.sqlite")  # persistent, so both scopes' indexes coexist on disk
    _seed_two_versions(root)

    # Index v2 first (its rows persist to the shared DB file), then open v1.
    ctx_v2 = ProjectContext.open(root, version="v2", sqlite_path=db)
    try:
        assert "e2" in ctx_v2.bundle.entities  # v2 sees its own content
    finally:
        ctx_v2.close()

    ctx_v1 = ProjectContext.open(root, version="v1", sqlite_path=db)
    try:
        # Residency: the v1 context carries ONLY v1's effective bundle; v2's e2 is never loaded.
        assert set(ctx_v1.bundle.entities) == {"e1"}
        # Its graph likewise holds only the active scope's nodes.
        assert not ctx_v1.graph.has_node("entity:e2")
        # And v1's retrieval never surfaces v2's on-disk rows (scope filter through ProjectContext).
        hits = ctx_v1.sqlite_store.search_content("OnlyInV2", limit=10)
        assert all("e2" not in str(hit.get("ref", "")) for hit in hits)
    finally:
        ctx_v1.close()
