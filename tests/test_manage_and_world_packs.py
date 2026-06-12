"""Round-11 surface: managed worlds + world packs (zip in/out) + archive manage actions
+ per-task provider timeout floors. All offline, $0."""

from __future__ import annotations

import io
import zipfile

import pytest

from owcopilot.app.actions import (
    _task_timeout_sec,
    delete_object_action,
    update_entity_action,
)
from owcopilot.app.workspaces import (
    create_managed_world,
    export_world_zip,
    import_world_zip,
    list_managed_worlds,
    sanitize_world_name,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.content.store import ContentStore


@pytest.fixture()
def world_root(tmp_path):
    root = tmp_path / "world_a"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="Mara", type=EntityType.NPC, description="Scout."
                ),
                "loc_fort": Entity(id="loc_fort", name="Fort", type=EntityType.LOCATION),
            },
            relations=[Relation(source="npc_mara", target="loc_fort", kind="located_in")],
            quests={
                "quest_patrol": Quest(id="quest_patrol", title="Patrol", objective="Walk the line.")
            },
        )
    )
    return root


# ------------------------------------------------------------------ managed worlds
def test_create_and_list_managed_worlds(tmp_path) -> None:
    base = tmp_path / "worlds"
    created = create_managed_world("盐汐群岛", base=base)
    assert created.name == "盐汐群岛"
    assert ContentStore(created).exists()
    names = [w["name"] for w in list_managed_worlds(base=base)]
    assert names == ["盐汐群岛"]
    with pytest.raises(ValueError, match="已存在"):
        create_managed_world("盐汐群岛", base=base)


def test_sanitize_world_name_strips_path_hostile_chars() -> None:
    assert sanitize_world_name('盐汐:群岛/"v2"') == "盐汐群岛v2"
    with pytest.raises(ValueError):
        sanitize_world_name("///")


# ------------------------------------------------------------------ world packs
def test_world_pack_round_trip(world_root, tmp_path) -> None:
    pack = export_world_zip(world_root)
    restored = import_world_zip(pack, "回流世界", base=tmp_path / "worlds")
    bundle = ContentStore(restored).load()
    assert set(bundle.entities) == {"npc_mara", "loc_fort"}
    assert set(bundle.quests) == {"quest_patrol"}
    assert len(bundle.relations) == 1


def test_world_pack_excludes_runtime_dir(world_root) -> None:
    runtime = world_root / ".owcopilot"
    runtime.mkdir()
    (runtime / "runtime.sqlite").write_bytes(b"not-portable")
    pack = export_world_zip(world_root)
    names = zipfile.ZipFile(io.BytesIO(pack)).namelist()
    assert not any(name.startswith(".owcopilot") for name in names)


def test_world_pack_import_strips_single_top_folder(world_root, tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pack:
        for name, data in [
            (
                "myworld/world/entities/npc_x.json",
                ContentStore(world_root).load().entities["npc_mara"].model_dump_json(),
            ),
        ]:
            pack.writestr(name, data)
    restored = import_world_zip(buffer.getvalue(), "nested", base=tmp_path / "worlds")
    assert (restored / "world" / "entities" / "npc_x.json").exists()


def test_world_pack_rejects_zip_slip(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pack:
        pack.writestr("../evil.txt", "boom")
    with pytest.raises(ValueError, match="非法路径"):
        import_world_zip(buffer.getvalue(), "evil", base=tmp_path / "worlds")
    assert not (tmp_path / "evil.txt").exists()


def test_world_pack_rejects_non_world_zip(tmp_path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as pack:
        pack.writestr("readme.txt", "hello")
    with pytest.raises(ValueError, match="世界包"):
        import_world_zip(buffer.getvalue(), "junk", base=tmp_path / "worlds")
    # failed import must roll back the half-made directory
    assert [w["name"] for w in list_managed_worlds(base=tmp_path / "worlds")] == []


# ------------------------------------------------------------------ manage actions
def test_update_entity_edits_fields_and_keeps_id(world_root) -> None:
    result = update_entity_action(
        str(world_root),
        entity_id="npc_mara",
        name="Mara the Bold",
        description="Veteran scout.",
        tags=["scout", " veteran ", ""],
    )
    assert result["entity"]["name"] == "Mara the Bold"
    assert result["entity"]["tags"] == ["scout", "veteran"]
    bundle = ContentStore(world_root).load()
    assert bundle.entities["npc_mara"].description == "Veteran scout."
    with pytest.raises(ValueError, match="不存在"):
        update_entity_action(str(world_root), entity_id="npc_ghost", name="X")


def test_delete_entity_cascades_relations_and_files(world_root) -> None:
    result = delete_object_action(
        str(world_root), ref_type="entity", object_id="npc_mara", cascade_relations=True
    )
    assert result["deleted_ref"] == "entity:npc_mara"
    assert result["removed_relations"] == 1
    bundle = ContentStore(world_root).load()
    assert "npc_mara" not in bundle.entities
    assert bundle.relations == []
    assert not (world_root / "world" / "entities" / "npc_mara.json").exists()


def test_delete_quest_and_unknown_type(world_root) -> None:
    deleted = delete_object_action(str(world_root), ref_type="quest", object_id="quest_patrol")
    assert deleted["deleted_ref"] == "quest:quest_patrol"
    assert "quest_patrol" not in ContentStore(world_root).load().quests
    with pytest.raises(ValueError, match="不支持"):
        delete_object_action(str(world_root), ref_type="poi", object_id="x")
    with pytest.raises(ValueError, match="不存在"):
        delete_object_action(str(world_root), ref_type="quest", object_id="quest_patrol")


# ------------------------------------------------------------------ timeout floors
def test_task_timeout_floors_and_user_override(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", raising=False)
    assert _task_timeout_sec("world_seed") == 240.0
    assert _task_timeout_sec("qa_answer") == 60.0
    monkeypatch.setenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "600")
    assert _task_timeout_sec("world_seed") == 600.0
    # a too-small user value never undercuts the per-task safety floor
    monkeypatch.setenv("OWCOPILOT_PROVIDER_TIMEOUT_SEC", "10")
    assert _task_timeout_sec("world_seed") == 240.0
