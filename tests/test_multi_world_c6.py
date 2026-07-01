"""Scale-P0 G2-C C6: multi-world switch. open_managed_world(name) opens a managed world by name as a
ProjectContext scoped to (world name, version) -- worlds are separate content roots (already handled
by create/list/delete/import/export in workspaces), so this ties that identity to the scope."""

from __future__ import annotations

import pytest

from owcopilot.app.workspaces import (
    create_managed_world,
    open_managed_world,
    worlds_home,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore


def _npc(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, type=EntityType.NPC)


def _seed(world_dir, entities: dict[str, str]) -> ContentStore:
    store = ContentStore(world_dir)
    bundle = ContentBundle()
    for eid, name in entities.items():
        bundle.entities[eid] = _npc(eid, name)
    store.save(bundle)
    return store


def test_open_managed_world_sets_scope_and_isolates(tmp_path) -> None:
    base = tmp_path / "worlds"
    create_managed_world("GameA", base=base)
    create_managed_world("GameB", base=base)
    _seed(worlds_home(base) / "GameA", {"a1": "A-npc"})

    ctx = open_managed_world("GameA", base=base)
    try:
        assert ctx.world_id == "GameA"  # scope reflects the world identity
        assert ctx.version == "v1"
        assert "a1" in ctx.bundle.entities
    finally:
        ctx.close()

    ctx_b = open_managed_world("GameB", base=base)
    try:
        assert ctx_b.world_id == "GameB"
        assert "a1" not in ctx_b.bundle.entities  # separate content root -> isolated
    finally:
        ctx_b.close()


def test_open_managed_world_selects_version(tmp_path) -> None:
    base = tmp_path / "worlds"
    create_managed_world("GameA", base=base)
    store = _seed(worlds_home(base) / "GameA", {"a1": "Alice"})
    store.create_version("v2", base_version="v1")
    b2 = store.load_scoped(version="v2")
    b2.entities["a2"] = _npc("a2", "OnlyV2")
    store.save_scoped(b2, version="v2")

    ctx = open_managed_world("GameA", version="v2", base=base)
    try:
        assert ctx.world_id == "GameA"
        assert ctx.version == "v2"
        assert "a2" in ctx.bundle.entities  # v2's copy-on-write effective content
    finally:
        ctx.close()


def test_open_managed_world_missing_raises(tmp_path) -> None:
    with pytest.raises(ValueError):
        open_managed_world("NoSuchWorld", base=tmp_path / "worlds")
