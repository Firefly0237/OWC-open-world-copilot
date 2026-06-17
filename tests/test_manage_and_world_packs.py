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
    delete_managed_world,
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


def test_world_name_cannot_escape_worlds_home(tmp_path) -> None:
    # a name is also a directory name — a traversal attempt must collapse to a plain name that
    # lands INSIDE worlds_home, never beside or above it.
    base = tmp_path / "worlds"
    for hostile in ("../evil", "..\\evil", "../../etc/passwd", "a/../b"):
        safe = sanitize_world_name(hostile)
        assert "/" not in safe and "\\" not in safe
        assert not safe.startswith("..")
    created = create_managed_world("../../etc/passwd", base=base)
    assert created.resolve().parent == base.resolve()  # stayed directly under worlds_home


def test_delete_managed_world_removes_only_its_own_dir(tmp_path) -> None:
    base = tmp_path / "worlds"
    keep = create_managed_world("留下的", base=base)
    create_managed_world("删掉的", base=base)
    delete_managed_world("删掉的", base=base)
    names = {w["name"] for w in list_managed_worlds(base=base)}
    assert names == {"留下的"}  # only the target is gone
    assert keep.exists()  # siblings untouched
    # a sentinel outside worlds_home must never be reachable, and a missing world is a clean error
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="不存在"):
        delete_managed_world("不存在的世界", base=base)
    assert (tmp_path / "outside.txt").exists()


def test_managed_worlds_are_isolated(tmp_path) -> None:
    base = tmp_path / "worlds"
    a = create_managed_world("世界甲", base=base)
    b = create_managed_world("世界乙", base=base)
    ContentStore(a).save(
        ContentBundle(entities={"npc_a": Entity(id="npc_a", name="甲人", type=EntityType.NPC)})
    )
    ContentStore(b).save(
        ContentBundle(entities={"npc_b": Entity(id="npc_b", name="乙人", type=EntityType.NPC)})
    )
    # each world sees only its own content; editing one never bleeds into the other
    assert set(ContentStore(a).load().entities) == {"npc_a"}
    assert set(ContentStore(b).load().entities) == {"npc_b"}
    # and a pack of A restores A's content alone, with no trace of B
    restored = import_world_zip(export_world_zip(a), "世界甲副本", base=base)
    assert set(ContentStore(restored).load().entities) == {"npc_a"}


# ------------------------------------------------------------------ world packs
def test_world_pack_round_trip(world_root, tmp_path) -> None:
    pack = export_world_zip(world_root)
    restored = import_world_zip(pack, "回流世界", base=tmp_path / "worlds")
    bundle = ContentStore(restored).load()
    assert set(bundle.entities) == {"npc_mara", "loc_fort"}
    assert set(bundle.quests) == {"quest_patrol"}
    assert len(bundle.relations) == 1


def test_world_pack_excludes_internal_dirs(world_root) -> None:
    # the pack is a portable handoff: runtime db, change-history snapshots and the git dir are all
    # internal/rebuildable and must never ride along (they bloat the pack and leak local state).
    for sub, payload in ((".owcopilot", "runtime.sqlite"), (".snapshots", "20260101.json")):
        (world_root / sub).mkdir()
        (world_root / sub / payload).write_bytes(b"not-portable")
    (world_root / ".git").mkdir()
    (world_root / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    names = zipfile.ZipFile(io.BytesIO(export_world_zip(world_root))).namelist()
    assert not any(name.startswith((".owcopilot", ".snapshots", ".git")) for name in names)
    assert any(name.startswith("world/") for name in names)  # real content still packed


def test_localization_only_pack_is_accepted(tmp_path) -> None:
    # regression: a pack whose only content is localized strings (or event refs) used to be read as
    # "empty" and rolled back, because _bundle_has_content omitted those collections.
    from owcopilot.content.models import LocalizedText

    root = tmp_path / "loc_world"
    ContentStore(root).save(
        ContentBundle(
            localized_texts={
                "t1": LocalizedText(id="t1", text_key="ui.start", locale="zh-CN", text="开始")
            }
        )
    )
    restored = import_world_zip(export_world_zip(root), "纯本地化", base=tmp_path / "worlds")
    assert set(ContentStore(restored).load().localized_texts) == {"t1"}


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


@pytest.mark.parametrize("name", ["CON", "NUL", "com1", "LPT9", "con.txt", "nul.json"])
def test_reserved_windows_device_names_rejected(name: str) -> None:
    # a world dir named CON/NUL fails or behaves pathologically on Windows (this app runs on win32)
    with pytest.raises(ValueError, match="系统保留名称"):
        sanitize_world_name(name)


def test_world_pack_rejects_decompression_bomb(tmp_path) -> None:
    # a pack that inflates past the cap must be rejected BEFORE anything is written to disk
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as pack:
        pack.writestr("world/entities/e1.json", b"{}")
        pack.writestr("world/bomb.bin", b"\x00" * (600 * 1024 * 1024))
    with pytest.raises(ValueError, match="体积异常巨大"):
        import_world_zip(buffer.getvalue(), "bomb", base=tmp_path / "worlds")
    assert not (tmp_path / "worlds" / "bomb").exists()  # nothing written


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
