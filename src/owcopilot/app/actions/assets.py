# 媒体资产引用：列出/挂接/摘除。
# Media asset references: list, attach, detach.

"""assets actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import open_project as _project


# >>> WS-I asset linking >>>
def asset_list_action(
    content_root: str | Path,
    *,
    object_ref: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...assets import AssetStore

    with _project(content_root, sqlite_path) as project:
        state = AssetStore(project.content_store.root).load()
        if object_ref is not None:
            return {
                "object_ref": object_ref,
                "assets": [a.model_dump(mode="json") for a in state.assets.get(object_ref, [])],
            }
        return {"assets": state.model_dump(mode="json")["assets"]}


def _object_ref_exists(bundle: Any, object_ref: str) -> bool:
    """Does `kind:id` point at an object that actually exists in canon? Guards against orphan
    assets attached to a typo'd / deleted object_ref."""
    kind, sep, oid = object_ref.partition(":")
    if not sep or not oid:
        return False
    table = {
        "entity": bundle.entities,
        "quest": bundle.quests,
        "region": bundle.regions,
        "poi": bundle.pois,
        "term": bundle.terms,
        "dialogue": bundle.dialogue_trees,
        "dialogue_tree": bundle.dialogue_trees,
    }.get(kind)
    return table is not None and oid in table


def asset_attach_action(
    content_root: str | Path,
    *,
    object_ref: str,
    kind: str,
    uri: str,
    title: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...assets import AssetKind, AssetStore, attach

    with _project(content_root, sqlite_path) as project:
        if not _object_ref_exists(project.bundle, object_ref):
            raise ValueError(f"找不到要挂接的对象「{object_ref}」，请先确认它已在正典中。")
        store = AssetStore(project.content_store.root)
        state = store.load()
        asset = attach(state, object_ref=object_ref, kind=AssetKind(kind), uri=uri, title=title)
        store.save(state)
        return {"asset": asset.model_dump(mode="json")}


def asset_detach_action(
    content_root: str | Path,
    *,
    asset_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    from ...assets import AssetStore, detach

    with _project(content_root, sqlite_path) as project:
        store = AssetStore(project.content_store.root)
        state = store.load()
        removed = detach(state, asset_id=asset_id)
        store.save(state)
        return {"removed": removed}
