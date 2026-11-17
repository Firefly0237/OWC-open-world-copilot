"""Conservative, explicit forks of a played narrative snapshot.

Immutable historical sessions remain readable. Continuing against a new project
requires a receipt proving that played scenes and their generation dependencies,
outgoing transitions, variable declarations and world identity did not change.
This is application compatibility checking, not an assertion that arbitrary
LangGraph state or fictional semantics can be migrated automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from ..content.hash import content_hash


def protected_contexts(
    project: dict[str, Any],
    scene_ids: list[str],
    signature: Callable[[str], str],
) -> dict[str, str]:
    outline = project["outline"]
    scenes = {row["id"]: row for row in outline["scenes"]}
    protected: dict[str, str] = {}

    def visit(scene_id: str, ancestors: frozenset[str]) -> None:
        if scene_id in ancestors:
            raise ValueError("场景生成依赖存在循环，不能迁移预演")
        if scene_id in protected:
            return
        if scene_id not in scenes or scene_id not in project["scene_bindings"]:
            raise ValueError("已玩场景或生成依赖缺少绑定：" + scene_id)
        scene = scenes[scene_id]
        binding = project["scene_bindings"][scene_id]
        actual = signature(scene_id)
        if actual != binding["input_signature"]:
            raise ValueError("已玩场景或生成依赖已失效：" + scene_id)
        if binding["binding_hash"] != content_hash(
            {key: value for key, value in binding.items() if key != "binding_hash"}
        ):
            raise ValueError("场景绑定hash不一致：" + scene_id)
        protected[scene_id] = content_hash(
            {
                "scene": scene,
                "binding_hash": binding["binding_hash"],
                "input_signature": actual,
                "outgoing": [row for row in outline["branches"] if row["source_scene"] == scene_id],
            }
        )
        for dependency in scene["dependencies"]:
            visit(dependency, ancestors | {scene_id})

    for scene_id in scene_ids:
        visit(scene_id, frozenset())
    return protected


def compatible_contexts(
    previous: dict[str, Any],
    current: dict[str, Any],
    session: dict[str, Any],
    signature: Callable[[str], str],
) -> dict[str, str]:
    for field in ("id", "project", "world_id", "base_version", "base_hash"):
        if previous.get(field) != current.get(field):
            raise ValueError("项目／世界基线改变，不能继承已玩路径：" + field)
    if previous["outline"]["variables"] != current["outline"]["variables"]:
        raise ValueError("项目变量合同改变，不能自动迁移已玩状态")
    frozen = session.get("protected_contexts")
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError("旧预演未记录依赖快照，不能证明安全继承；原历史仍可读取")
    actual = protected_contexts(current, session["visited_scenes"], signature)
    if frozen != actual:
        changed = sorted(
            key for key in frozen.keys() | actual.keys() if frozen.get(key) != actual.get(key)
        )
        raise ValueError("已玩内容／生成依赖／分支发生变化，不能无声改写：" + ",".join(changed))
    return actual


def compatible_edit_projection(previous, current, session, signature, db):
    """Allow an explicit text application in the current scene, retaining all history."""
    from .edit_continuity import application_chain, project_domain_projection

    for field in ("id", "project", "world_id", "base_version", "base_hash"):
        if previous.get(field) != current.get(field):
            raise ValueError("项目／世界基线改变，不能继承已玩路径：" + field)
    if previous["outline"]["variables"] != current["outline"]["variables"]:
        raise ValueError("项目变量合同改变，不能继承原状态")
    scene_id = session["scene_id"]
    if session["visited_scenes"].count(scene_id) > 1:
        raise ValueError("当前场景已经多次经过，无法核实每次已确认正文；请保留旧版或从新版入口开始")
    old = previous["scene_bindings"].get(scene_id)
    new = current["scene_bindings"].get(scene_id)
    if not old or not new or old["artifact_id"] == new["artifact_id"]:
        raise ValueError("当前场景没有可继承的正文应用变更")
    chain = application_chain(db, current["project"], old["artifact_id"], new["artifact_id"])
    if not chain:
        raise ValueError("当前场景变更缺少明确的正文应用来源链")
    ignored = {
        "artifact_id",
        "artifact_hash",
        "task_revision",
        "payload",
        "binding_hash",
        "requirement_report",
        "application_chain",
    }
    if {key: value for key, value in old.items() if key not in ignored} != {
        key: value for key, value in new.items() if key not in ignored
    }:
        raise ValueError("当前场景生成来源或状态上下文已改变")
    # The existing full-context check still protects every scene definition,
    # outgoing edge and generation dependency. Only this one verified binding
    # is masked to its exact original value for the comparison.
    masked = deepcopy(current)
    masked["scene_bindings"][scene_id] = old
    compatible_contexts(previous, masked, session, signature)
    domain, evidence = project_domain_projection(session["domain"], old["payload"], new["payload"])
    contexts = protected_contexts(current, session["visited_scenes"], signature)
    return (
        contexts,
        domain,
        {
            **evidence,
            "application_ids": [row["id"] for row in chain],
            "source_artifact_id": old["artifact_id"],
            "artifact_id": new["artifact_id"],
            "protected_scenes": sorted(contexts),
        },
    )
