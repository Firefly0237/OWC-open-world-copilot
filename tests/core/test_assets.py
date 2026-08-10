"""WS-I · asset linking: attach existing media references to objects, detach, list."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import asset_attach_action, asset_detach_action, asset_list_action
from owcopilot.assets import AssetKind, AssetState, attach, detach
from owcopilot.content.models import (
    ContentBundle,
    DialogueRef,
    DialogueTree,
    Entity,
    EntityType,
    Relation,
    StyleGuide,
    StyleRubric,
)
from owcopilot.content.store import ContentStore


def test_attach_is_idempotent_and_detach_works() -> None:
    state = AssetState()
    a = attach(
        state, object_ref="entity:npc_x", kind=AssetKind.IMAGE, uri="art/x.png", title="立绘"
    )
    attach(state, object_ref="entity:npc_x", kind=AssetKind.IMAGE, uri="art/x.png")  # same -> dedup
    assert len(state.assets["entity:npc_x"]) == 1
    assert detach(state, asset_id=a.id) is True
    assert "entity:npc_x" not in state.assets  # last asset removed -> thread cleared


def test_empty_uri_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        attach(AssetState(), object_ref="entity:x", kind=AssetKind.LINK, uri="   ")


def test_trailing_whitespace_uri_is_still_deduped() -> None:
    # D3: hash must key on the NORMALIZED uri, or a stray space leaks a duplicate
    st = AssetState()
    attach(st, object_ref="entity:x", kind=AssetKind.IMAGE, uri="art/x.png")
    attach(st, object_ref="entity:x", kind=AssetKind.IMAGE, uri="art/x.png ")
    assert len(st.assets["entity:x"]) == 1


def test_overlong_uri_rejected_and_control_chars_stripped() -> None:
    # D5: length cap + control-char stripping
    with pytest.raises(ValueError, match="过长"):
        attach(AssetState(), object_ref="entity:x", kind=AssetKind.LINK, uri="x" * 3000)
    a = attach(AssetState(), object_ref="entity:x", kind=AssetKind.LINK, uri="http://x\n\t\x00y")
    assert "\n" not in a.uri and "\x00" not in a.uri


def test_asset_actions_persist(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(entities={"npc_x": Entity(id="npc_x", name="甲", type=EntityType.NPC)})
    )
    res = asset_attach_action(
        root, object_ref="entity:npc_x", kind="map", uri="maps/region.png", title="地图"
    )
    aid = res["asset"]["id"]
    listed = asset_list_action(root, object_ref="entity:npc_x")
    assert listed["assets"][0]["uri"] == "maps/region.png"
    assert asset_detach_action(root, asset_id=aid)["removed"] is True
    assert asset_list_action(root, object_ref="entity:npc_x")["assets"] == []


def test_attach_to_nonexistent_object_rejected(tmp_path) -> None:
    # D4: attaching to a typo'd / missing object_ref must be a clean error, not an orphan asset
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(entities={"npc_x": Entity(id="npc_x", name="甲", type=EntityType.NPC)})
    )
    with pytest.raises(ValueError, match="找不到要挂接的对象"):
        asset_attach_action(root, object_ref="entity:ghost", kind="image", uri="art/x.png")


def test_asset_actions_accept_non_entity_canon_refs(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_x": Entity(id="npc_x", name="甲", type=EntityType.NPC),
                "npc_y": Entity(id="npc_y", name="乙", type=EntityType.NPC),
            },
            relations=[Relation(source="npc_x", kind="knows", target="npc_y")],
            dialogues={"line_x": DialogueRef(id="line_x", text_key="line.x", text="你好")},
            dialogue_trees={"tree_x": DialogueTree(id="tree_x")},
            style_guides={"main": StyleGuide(id="main", body="克制")},
            style_rubrics={"rubric_x": StyleRubric(id="rubric_x", label="冷峻")},
        )
    )

    refs = [
        "relation:npc_x:knows:npc_y",
        "dialogue:line_x",
        "dialogue_tree:tree_x",
        "style_guide:main",
        "style_rubric:rubric_x",
    ]
    for index, object_ref in enumerate(refs):
        result = asset_attach_action(
            root,
            object_ref=object_ref,
            kind="link",
            uri=f"https://example.test/{index}",
        )
        assert result["asset"]["object_ref"] == object_ref
