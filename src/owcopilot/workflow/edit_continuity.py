"""Exact draft-application facts and conservative continuation of narrative sessions."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Self

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from ..content.models import DialogueTree, Quest
from ..logic.stepper import PreviewSession, preview_view
from .anchors import FeedbackAnchor, resolve_feedback_anchor
from .intent_models import IntentContract
from .intent_review import _diff
from .intent_store import _decode, _sealed
from .models import Artifact, TaskRecord, new_id, now
from .store import WorkflowStore


class ApplicationContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reading_path: str | None = Field(default=None, min_length=1, max_length=2048)
    task_preview_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_preview_seq: int | None = Field(default=None, ge=0)
    narrative_project_id: str | None = Field(default=None, min_length=1, max_length=128)
    narrative_scene_id: str | None = Field(default=None, min_length=1, max_length=128)
    narrative_project_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project_preview_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_preview_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def paired(self) -> Self:
        for group in (
            (self.task_preview_id, self.task_preview_seq),
            (self.narrative_project_id, self.narrative_scene_id, self.narrative_project_hash),
            (self.project_preview_id, self.project_preview_hash),
        ):
            if any(value is not None for value in group) and any(value is None for value in group):
                raise ValueError("继续创作的位置与版本字段必须成组提供")
        if self.project_preview_id and not self.narrative_project_id:
            raise ValueError("项目试玩需要明确作品和场景")
        if self.task_preview_id and self.project_preview_id:
            raise ValueError("一次应用只能绑定一种原试玩位置")
        return self


def _artifact(db, project: str, artifact_id: str) -> Artifact:
    row = db.execute("SELECT payload FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        raise KeyError("应用关联的产物不存在")
    artifact = Artifact.model_validate_json(row[0])
    task = db.execute("SELECT payload FROM tasks WHERE id=?", (artifact.task_id,)).fetchone()
    if task is None or json.loads(task[0])["project"] != project:
        raise KeyError("应用关联的产物不属于当前工作区")
    if content_hash(artifact.payload) != artifact.content_hash:
        raise ValueError("应用关联的产物hash不一致")
    return artifact


def _value(document: Any, parts: tuple[str, ...]) -> tuple[bool, Any]:
    value = document
    try:
        for part in parts:
            value = value[int(part)] if isinstance(value, list) else value[part]
        return True, value
    except (KeyError, IndexError, ValueError, TypeError):
        return False, None


def artifact_changes(before: dict, after: dict) -> list[dict[str, Any]]:
    result = []
    for parts in sorted(_diff(before, after)):
        left, old = _value(before, parts)
        right, new = _value(after, parts)
        path = "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)
        result.append(
            {
                "path": path,
                "before": old,
                "after": new,
                "before_exists": left,
                "after_exists": right,
            }
        )
    return result


def project_review_changes(payload: dict, before: dict, after: dict) -> dict:
    """Preserve unchanged source serialization, including absent legacy defaults."""
    result = deepcopy(payload)
    roots = {("quests", payload["quest"]["id"]): "quest"}
    if payload.get("tree"):
        roots[("dialogue_trees", payload["tree"]["id"])] = "tree"
    for parts in sorted(_diff(before, after)):
        root = roots.get(parts[:2])
        if root is None or len(parts) < 3:
            raise ValueError("候选包含当前草稿字段之外的改动")
        path = (root, *parts[2:])
        current = result
        for part in path[:-1]:
            current = current[int(part)] if isinstance(current, list) else current[part]
        exists, value = _value(after, parts)
        key = int(path[-1]) if isinstance(current, list) else path[-1]
        if exists:
            current[key] = deepcopy(value)
        else:
            del current[key]
    return result


def text_only_changes(before: dict, after: dict) -> list[dict[str, Any]]:
    changes = artifact_changes(before, after)
    for row in changes:
        try:
            # The anchor resolver's routing and strict pointer contract is shared
            # with the application UI; it does not grant edit authority.
            from .anchors import _pointer_tokens, _route

            parts = _pointer_tokens(row["path"])
            _route("bound_dialogue", parts)
        except ValueError as exc:
            raise ValueError("继续位置只支持正文修改，机制或结构已改变：" + row["path"]) from exc
        if (
            not row["before_exists"]
            or not row["after_exists"]
            or not all(isinstance(row[side], str) for side in ("before", "after"))
        ):
            raise ValueError("继续位置不能新增、删除或改型正文位置：" + row["path"])
    return changes


def application_chain(db, project: str, source_id: str, target_id: str) -> list[dict] | None:
    if source_id == target_id:
        return []
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='creative_intent_applications'"
    ).fetchone():
        return None
    records = [
        _decode(row[0], "receipt_hash")
        for row in db.execute(
            "SELECT payload FROM creative_intent_applications WHERE project=?", (project,)
        )
    ]
    chain: list[dict] = []
    seen: set[str] = set()
    cursor = target_id
    while cursor != source_id:
        if cursor in seen or len(seen) >= 100:
            raise ValueError("应用来源链循环或超出检查边界")
        seen.add(cursor)
        matches = [row for row in records if row["artifact_id"] == cursor]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("应用来源链存在歧义")
        receipt = matches[0]
        before = _artifact(db, project, receipt["source_artifact_id"])
        after = _artifact(db, project, receipt["artifact_id"])
        review = db.execute(
            "SELECT payload FROM creative_intent_reviews WHERE id=? AND project=?",
            (receipt["review_id"], project),
        ).fetchone()
        if review is None:
            raise ValueError("应用缺少来源审查")
        reviewed = _decode(review[0], "review_hash")
        if (
            before.task_id != after.task_id
            or before.task_id != receipt["task_id"]
            or reviewed["review_hash"] != receipt["review_hash"]
            or reviewed["baseline_artifact_id"] != before.id
            or reviewed["baseline_artifact_hash"] != before.content_hash
        ):
            raise ValueError("应用来源身份与实际产物不一致")
        text_only_changes(before.payload, after.payload)
        chain.append(receipt)
        cursor = before.id
    return list(reversed(chain))


def check_patch_narrative_snapshot(
    store: WorkflowStore, project: str, state: dict, task: TaskRecord, baseline: Artifact
) -> dict | None:
    """Admit a local proposal after this run's own artifact was explicitly bound."""
    if not state.get("narrative_project_id"):
        return None
    from .project_snapshot_v2 import protected_contexts
    from .project_v2 import NarrativeProjectStore, SourceAnchor
    from .project_v2 import _decode as decode_project
    from .v2_service import check_narrative_snapshot

    narrative = NarrativeProjectStore(store)
    frozen = state["narrative_context"]
    current = narrative.get(project, state["narrative_project_id"])
    if current["project_hash"] == frozen.get("project_hash"):
        check_narrative_snapshot(store, project, state)
        return {"policy": "exact_project_snapshot", "project_hash": current["project_hash"]}
    if (
        state.get("project") != project
        or state.get("task_id") != task.id
        or state.get("run_id") != baseline.run_id
        or task.project != project
        or baseline.task_id != task.id
        or content_hash(baseline.payload) != baseline.content_hash
        or frozen.get("generation_context_hash")
        != content_hash(
            {key: value for key, value in frozen.items() if key != "generation_context_hash"}
        )
    ):
        raise ValueError("局部建议的原生成身份或冻结上下文不一致")
    scene_id = state.get("narrative_scene_id")
    if (
        not scene_id
        or frozen.get("project_id") != current["id"]
        or frozen.get("scene", {}).get("id") != scene_id
        or frozen.get("generation_binding", {}).get("project_hash") != frozen["project_hash"]
    ):
        raise ValueError("局部建议的场景来源不一致")
    world = {
        "world_id": task.request.world_id,
        "base_version": task.base_version,
        "base_hash": task.base_hash,
    }
    expected_scope = {
        "context_hash": frozen["generation_context_hash"],
        "generation_binding": frozen["generation_binding"],
        "path_state": frozen["path_state"],
        "branch_history": frozen["branch_history"],
        "policy": "exact_frozen_path_and_project_state",
    }
    with store.connection() as db:
        db.execute("BEGIN")
        current = narrative._project(db, project, current["id"])
        previous = narrative._historical_project(db, current, frozen)
        distance = current["version"] - previous["version"]
        if not 0 < distance <= 100:
            raise ValueError("局部建议的发布版本链缺失或超过100版检查范围")
        source = state.get("feedback_source") or {}
        if not source.get("artifact_id"):
            raise ValueError("局部建议缺少原运行实际发布产物的证据")
        original = _artifact(db, project, source["artifact_id"])
        if (
            source.get("artifact_hash") != original.content_hash
            or source.get("kind") != original.kind
            or original.task_id != task.id
            or original.run_id != baseline.run_id
            or original.revision != state.get("revision")
            or (baseline.node_id, baseline.kind) != (original.node_id, original.kind)
        ):
            raise ValueError("局部建议的实际发布产物与生成运行不一致")
        lineage = application_chain(db, project, original.id, baseline.id)
        if lineage is None:
            raise ValueError("局部建议基线不是原发布产物或明确的正文应用后继")
        rows = db.execute(
            "SELECT version,payload FROM narrative_project_versions_v2 "
            "WHERE project_id=? AND version>? AND version<=? ORDER BY version LIMIT 101",
            (current["id"], previous["version"], current["version"]),
        ).fetchall()
        if len(rows) != distance:
            raise ValueError("局部建议的项目发布版本链不完整")
        ignored = {"project_hash", "version", "parent_hash", "updated_at", "scene_bindings", *world}
        prior_artifact = None
        for index, row in enumerate(rows):
            version = decode_project(row["payload"], "project_hash")
            if (
                row["version"] != version["version"]
                or version["version"] != previous["version"] + 1
                or version["parent_hash"] != previous["project_hash"]
                or {key: value for key, value in version.items() if key not in ignored}
                != {key: value for key, value in previous.items() if key not in ignored}
                or {
                    key: value
                    for key, value in version["scene_bindings"].items()
                    if key != scene_id
                }
                != {
                    key: value
                    for key, value in previous["scene_bindings"].items()
                    if key != scene_id
                }
                or any(version.get(key) != value for key, value in world.items())
                or not (
                    all(previous.get(key) == value for key, value in world.items())
                    or index == 0
                    and all(previous.get(key) is None for key in world)
                )
            ):
                raise ValueError("原生成后存在场景自产物绑定之外的项目修改")
            binding = version["scene_bindings"].get(scene_id)
            if not binding or binding == previous["scene_bindings"].get(scene_id):
                raise ValueError("发布版本并非同一场景产物的明确绑定")
            decode_project(json.dumps(binding), "binding_hash")
            artifact = _artifact(db, project, binding["artifact_id"])
            if (
                (artifact.task_id, artifact.run_id, artifact.node_id, artifact.kind)
                != (task.id, original.run_id, original.node_id, original.kind)
                or binding.get("artifact_hash") != artifact.content_hash
                or binding.get("payload") != artifact.payload
                or binding.get("task_id") != task.id
                or binding.get("task_revision") != artifact.revision
                or binding.get("input_signature") != frozen["generation_binding"]["input_signature"]
                or binding.get("generation_scope") != expected_scope
                or binding.get("reference_snapshot") != state.get("references", [])
            ):
                raise ValueError("发布绑定的实际产物、来源或生成路径不一致")
            chain = application_chain(
                db, project, prior_artifact.id if prior_artifact else original.id, artifact.id
            )
            if chain is None or prior_artifact and not chain:
                raise ValueError("发布绑定没有向前的明确正文应用链")
            if prior_artifact and binding.get("application_chain") != [r["id"] for r in chain]:
                raise ValueError("发布绑定与实际正文应用回执不一致")
            prior_artifact, previous = artifact, version
        if previous != current or prior_artifact is None or prior_artifact.id != baseline.id:
            raise ValueError("当前场景未绑定本次局部建议的同一基线产物")
        signature = narrative._signature(db, current, scene_id)
        if signature != frozen["generation_binding"]["input_signature"]:
            raise ValueError("局部建议的当前来源、事实或前史依赖已变化")
        actual_context = narrative.project_context(
            project,
            current["id"],
            scene_id,
            branch_history=frozen["branch_history"],
            values=frozen["path_state"],
        )
        context_ignored = {
            "project_hash",
            "generation_binding",
            "history_dependencies",
            "generation_context_hash",
        }
        if actual_context["project_hash"] != current["project_hash"] or {
            key: value for key, value in actual_context.items() if key not in context_ignored
        } != {key: value for key, value in frozen.items() if key not in context_ignored}:
            raise ValueError("局部建议的资料、事实或原路径上下文已变化")
        dependencies = []
        scenes = {row["id"]: row for row in current["outline"]["scenes"]}
        protected_contexts(
            current,
            scenes[scene_id]["dependencies"],
            lambda key: narrative._signature(db, current, key),
        )
        for key in scenes[scene_id]["dependencies"]:
            dependency = current["scene_bindings"][key]
            dependencies.append(
                {
                    "scene_id": key,
                    **{
                        field: dependency[field]
                        for field in (
                            "artifact_id",
                            "artifact_hash",
                            "binding_hash",
                            "input_signature",
                            "payload",
                        )
                    },
                    "sources": [
                        narrative._anchor(
                            db, current, SourceAnchor.model_validate(anchor), use="drafting"
                        )
                        for anchor in scenes[key]["sources"]
                    ],
                }
            )
        if dependencies != frozen.get("history_dependencies"):
            raise ValueError("局部建议的实际前史依赖不等于原生成输入")
        return {
            "policy": "own_published_artifact_and_text_applications_v1",
            "source_project_hash": frozen["project_hash"],
            "project_hash": current["project_hash"],
            "scene_id": scene_id,
            "source_artifact_id": original.id,
            "artifact_id": baseline.id,
            "application_ids": [row["id"] for row in lineage],
            "publication_versions": distance,
        }


def validate_continuation(
    db: Any, project: str, task: TaskRecord, baseline: Artifact, value: dict | None
) -> dict[str, Any]:
    request = ApplicationContinuation.model_validate(value or {})
    result = request.model_dump(exclude_none=True)
    preview = None
    if request.task_preview_id:
        row = db.execute(
            "SELECT payload FROM previews WHERE id=?", (request.task_preview_id,)
        ).fetchone()
        if row is None:
            raise KeyError("原任务试玩不存在")
        preview = json.loads(row[0])
        if preview.get("version_mode") == "fixed_historical":
            raise ValueError("旧版隔离试玩不能作为当前修改的位置输入")
        if (
            preview.get("task_id") != task.id
            or preview.get("project") != project
            or preview.get("revision") != task.revision
            or preview.get("artifact_id") != baseline.id
            or preview["session"]["action_seq"] != request.task_preview_seq
        ):
            raise ValueError("原试玩位置与本次应用的来源或版本不一致")
        result["task_preview_hash"] = content_hash(preview)
    if request.reading_path:
        anchor = FeedbackAnchor(
            artifact_id=baseline.id, artifact_hash=baseline.content_hash, path=request.reading_path
        )
        resolve_feedback_anchor(task, baseline, anchor)
    if request.narrative_project_id:
        from .project_v2 import NarrativeProjectStore
        from .project_v2 import _decode as decode_project

        current = NarrativeProjectStore._project(db, project, request.narrative_project_id)
        if current["project_hash"] != request.narrative_project_hash:
            raise ValueError("应用前作品版本已改变")
        if request.narrative_scene_id not in {row["id"] for row in current["outline"]["scenes"]}:
            raise ValueError("应用定位的场景不存在")
        binding = current["scene_bindings"].get(request.narrative_scene_id)
        if binding and binding["artifact_id"] != baseline.id:
            raise ValueError("指定场景并未绑定本次原稿")
        result["source_binding_hash"] = binding["binding_hash"] if binding else None
        if request.project_preview_id:
            row = db.execute(
                "SELECT payload FROM narrative_previews_v2 "
                "WHERE id=? AND project=? AND project_id=?",
                (request.project_preview_id, project, request.narrative_project_id),
            ).fetchone()
            if row is None:
                raise KeyError("原项目试玩不存在")
            session = decode_project(row[0], "preview_hash")
            if session.get("version_mode") == "fixed_historical":
                raise ValueError("旧版隔离试玩不能作为当前修改的位置输入")
            if (
                session["preview_hash"] != request.project_preview_hash
                or session["project_hash"] != current["project_hash"]
                or session["scene_id"] != request.narrative_scene_id
                or not binding
            ):
                raise ValueError("原项目试玩的位置或来源版本已改变")
    return result


def _production_impact(db: Any, task: TaskRecord, baseline: Artifact, feedback: dict) -> dict:
    from ..app.actions.workflow import artifact_is_stale

    result = {
        "basis": "unknown",
        "source_node": baseline.node_id,
        "affected_nodes": [],
        "affected_operations": feedback["affected_operations"],
        "stale_artifacts": [],
        "unknown_reasons": [],
    }
    table = db.execute("SELECT 1 FROM sqlite_master WHERE name='creative_v2_runs'").fetchone()
    row = (
        db.execute(
            "SELECT payload FROM creative_v2_runs WHERE run_id=?", (baseline.run_id,)
        ).fetchone()
        if table
        else None
    )
    if row and (graph := json.loads(row[0]).get("graph")):
        nodes = {node["id"]: node for node in graph["nodes"]}
        if baseline.node_id in nodes:
            dependency = nx.DiGraph()
            dependency.add_nodes_from(nodes)
            dependency.add_edges_from(
                (edge["source_node"], edge["target_node"])
                for edge in graph["data_edges"] + graph["control_edges"]
            )
            affected = nx.descendants(dependency, baseline.node_id) | {baseline.node_id}
            result["basis"] = "stored_production_graph"
            result["affected_nodes"] = [
                {"id": node["id"], "operation": node["operation"]}
                for node in nodes.values()
                if node["id"] in affected
            ]
    if result["basis"] == "unknown":
        result["unknown_reasons"] = ["没有能与原产物节点对应的制作图；失效范围采用已保存的修订规则"]
    for row in db.execute("SELECT payload FROM artifacts WHERE task_id=?", (task.id,)):
        artifact = Artifact.model_validate_json(row[0])
        if artifact_is_stale(task, artifact):
            result["stale_artifacts"].append(
                {"id": artifact.id, "hash": artifact.content_hash, "kind": artifact.kind}
            )
    return result


def application_impact(
    db: Any,
    task: TaskRecord,
    baseline: Artifact,
    artifact: Artifact,
    contract: IntentContract,
    review: dict,
    feedback: dict,
    continuation: dict,
) -> dict:
    path = continuation.get("reading_path")
    parts: tuple[str, ...] = ()
    if path:
        from .anchors import _pointer_tokens

        parts = tuple(_pointer_tokens(path))
    old_exists = _value(baseline.payload, parts)[0] if path else False
    new_exists = _value(artifact.payload, parts)[0] if path else False
    changes = artifact_changes(baseline.payload, artifact.payload)
    return {
        "schema_version": 1,
        "kind": "content_application",
        "source": {
            "artifact_id": baseline.id,
            "artifact_hash": baseline.content_hash,
            "task_revision": baseline.revision,
        },
        "target": {
            "artifact_id": artifact.id,
            "artifact_hash": artifact.content_hash,
            "task_revision": artifact.revision,
        },
        "reason": {
            "text": contract.modification_goal,
            "contract_id": contract.id,
            "contract_hash": review["contract_hash"],
        },
        "scope": {
            "allowed_paths": list(contract.allowed_paths),
            "hard_locks": [row.model_dump(mode="json") for row in contract.hard_locks],
            "formal_rule_count": len(contract.formal_rules),
        },
        "changes": changes,
        "production": _production_impact(db, task, baseline, feedback),
        "reading": {
            "path": path,
            "source_exists": old_exists,
            "target_exists": new_exists,
            "current_text_will_change": any(row["path"] == path for row in changes),
            "human_reading_state": "not_inferred",
        },
        "playtest": {
            "status": "needs_binding"
            if continuation.get("narrative_project_id")
            else "eligible_for_check"
            if continuation.get("task_preview_id")
            else "not_requested",
            "source": continuation,
        },
        "unconfirmed": [
            {"kind": "literary_intention", "text": text}
            for text in [contract.modification_goal, *contract.soft_text]
            if text
        ],
        "review_id": review["id"],
        "review_hash": review["review_hash"],
    }


def project_domain_projection(session: dict, source: dict, target: dict) -> tuple[dict, dict]:
    """Copy only uncommitted text into a new runtime identity, without executing it."""
    changes = text_only_changes(source, target)
    current = PreviewSession.model_validate(session)
    if (current.quest and current.quest.model_dump(mode="json") != source.get("quest")) or (
        current.dialogue_tree
        and current.dialogue_tree.model_dump(mode="json") != source.get("tree")
    ):
        raise ValueError("原试玩运行对象与原稿不一致")
    committed_dialogue = {step.cursor for step in current.history if step.phase == "dialogue"}
    committed_quest = {step.cursor for step in current.history if step.phase == "quest"}
    from .anchors import _pointer_tokens

    for row in changes:
        parts = _pointer_tokens(row["path"])
        if parts[:2] == ["tree", "nodes"] and parts[2] in committed_dialogue:
            raise ValueError("已经确认推进的对白或选项已改变，不能继承原路径：" + parts[2])
        if parts[:2] == ["quest", "stages"]:
            if source["quest"]["stages"][int(parts[2])]["id"] in committed_quest:
                raise ValueError("已经确认推进的阶段正文已改变，不能继承原路径")
    projected = current.model_copy(deep=True)
    projected.id = new_id()
    projected.quest = Quest.model_validate(target["quest"]) if target.get("quest") else None
    projected.dialogue_tree = (
        DialogueTree.model_validate(target["tree"]) if target.get("tree") else None
    )
    projected.applied_actions = {}
    # No state transition is run here. Structural equality outside text preserves
    # IDs, conditions, effects, routes and exact ending bindings.
    return projected.model_dump(mode="json"), {
        "policy": "explicit_application_uncommitted_text_v1",
        "changes": changes,
        "committed_dialogue_nodes": sorted(committed_dialogue),
        "committed_quest_stages": sorted(committed_quest),
        "current_text_will_change": preview_view(current)["text"]
        != preview_view(projected)["text"],
        "human_reading_state": "not_inferred",
    }


def _application(db, project: str, application_id: str) -> dict:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='creative_intent_applications'"
    ).fetchone():
        raise KeyError("应用回执不存在")
    rows = db.execute(
        "SELECT payload FROM creative_intent_applications WHERE project=?", (project,)
    )
    for row in rows:
        receipt = _decode(row[0], "receipt_hash")
        if receipt["id"] == application_id:
            return receipt
    raise KeyError("应用回执不属于当前工作区或不存在")


def list_applications(store: WorkflowStore, project: str, task_id: str) -> dict:
    task = store.get_task(task_id)
    if task.project != project:
        raise KeyError("任务不属于当前工作区")
    with store.connection() as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='creative_intent_applications'"
        ).fetchone():
            return {"applications": []}
        return {
            "applications": [
                _decode(row[0], "receipt_hash")
                for row in db.execute(
                    "SELECT payload FROM creative_intent_applications "
                    "WHERE project=? AND task_id=? ORDER BY rowid",
                    (project, task_id),
                )
            ]
        }


def read_application(store: WorkflowStore, project: str, application_id: str) -> dict:
    from ..app.actions.workflow import artifact_is_stale, task_summary

    with store.connection() as db:
        receipt = _application(db, project, application_id)
        artifact = _artifact(db, project, receipt["artifact_id"])
        task = store.get_task(receipt["task_id"])
        return {
            "receipt": receipt,
            "artifact": artifact.model_dump(mode="json"),
            "task": task_summary(task, store),
            "impact": receipt.get("impact"),
            "target_current": not artifact_is_stale(task, artifact),
        }


def continue_application_preview(
    store: WorkflowStore,
    project: str,
    application_id: str,
    *,
    preview_id: str,
    expected_seq: int,
) -> dict:
    from ..app.actions.workflow import artifact_is_stale
    from .reference_guard_v2 import check_reference_snapshot

    with store.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS creative_edit_preview_forks(
            command_key TEXT PRIMARY KEY, project TEXT NOT NULL, payload TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        receipt = _application(db, project, application_id)
        row = db.execute("SELECT payload FROM previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise KeyError("原任务试玩不存在")
        source = json.loads(row[0])
        if source.get("version_mode") == "fixed_historical":
            raise ValueError("旧版隔离试玩不能迁移为当前创作输入")
        if source.get("project") != project or source.get("task_id") != receipt["task_id"]:
            raise ValueError("原试玩不属于本次应用任务")
        if source["session"]["action_seq"] != expected_seq:
            raise ValueError("原试玩位置已改变")
        key = content_hash([application_id, preview_id, content_hash(source)])
        saved = db.execute(
            "SELECT payload FROM creative_edit_preview_forks WHERE command_key=? AND project=?",
            (key, project),
        ).fetchone()
        if saved:
            return _decode(saved[0], "migration_hash")["result"]
        task_row = db.execute(
            "SELECT payload FROM tasks WHERE id=?", (receipt["task_id"],)
        ).fetchone()
        task = TaskRecord.model_validate_json(task_row[0])
        target = _artifact(db, project, receipt["artifact_id"])
        baseline = _artifact(db, project, source["artifact_id"])
        if artifact_is_stale(task, target):
            raise ValueError("应用后的草稿已失效，请读取当前版本")
        chain = application_chain(db, project, baseline.id, target.id)
        if not chain:
            raise ValueError("原试玩与应用后草稿没有可核验的正文应用链")
        state_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='creative_v2_runs'"
        ).fetchone()
        state = (
            db.execute(
                "SELECT payload FROM creative_v2_runs WHERE run_id=?", (baseline.run_id,)
            ).fetchone()
            if state_table
            else None
        )
        if state:
            check_reference_snapshot(
                store.path.parent.parent, json.loads(state[0]).get("references", [])
            )
        domain, evidence = project_domain_projection(
            source["session"], baseline.payload, target.payload
        )
        record = {
            **source,
            "revision": task.revision,
            "artifact_id": target.id,
            "session": domain,
            "parent_preview_id": preview_id,
            "application_id": application_id,
        }
        migration = _sealed(
            {
                "id": new_id(),
                "source_preview_id": preview_id,
                "application_ids": [item["id"] for item in chain],
                "source_artifact_id": baseline.id,
                "artifact_id": target.id,
                "evidence": evidence,
                "created_at": now(),
            },
            "migration_hash",
        )
        record["snapshot_migration"] = migration
        result = {
            **preview_view(domain),
            "preview_id": domain["id"],
            **{key: value for key, value in record.items() if key != "session"},
            "snapshot_migration": migration,
        }
        db.execute(
            "INSERT INTO previews VALUES(?,?)",
            (domain["id"], json.dumps(record, ensure_ascii=False)),
        )
        stored = _sealed({"result": result}, "migration_hash")
        db.execute(
            "INSERT INTO creative_edit_preview_forks VALUES(?,?,?)",
            (key, project, json.dumps(stored, ensure_ascii=False)),
        )
        return result


def _task_preview_result(record: dict) -> dict:
    return {
        **preview_view(record["session"]),
        "preview_id": record["session"]["id"],
        **{key: value for key, value in record.items() if key != "session"},
    }


def read_task_preview(store: WorkflowStore, project: str, preview_id: str) -> dict:
    record = store.get_preview(preview_id)
    if record.get("project") != project:
        raise KeyError("试玩不属于当前工作区")
    return _task_preview_result(record)


def _verify_original_task_preview(
    db: Any,
    store: WorkflowStore,
    project: str,
    record: dict,
) -> TaskRecord:
    from .reference_guard_v2 import check_reference_snapshot

    if record.get("project") != project or not record.get("artifact_id"):
        raise ValueError("旧版试玩缺少明确的来源产物")
    artifact = _artifact(db, project, record["artifact_id"])
    task = TaskRecord.model_validate_json(
        db.execute("SELECT payload FROM tasks WHERE id=?", (record["task_id"],)).fetchone()[0]
    )
    if artifact.task_id != task.id or artifact.revision > record["revision"]:
        raise ValueError("旧版试玩的产物与任务版本不一致")
    if record.get("frozen_base") and record["frozen_base"] != {
        "base_hash": task.base_hash,
        "base_version": task.base_version,
        "world_id": task.request.world_id,
    }:
        raise ValueError("任务的世界基线已改变，旧版试玩不能继续")
    # The empty projection validates the exact runtime objects without advancing.
    project_domain_projection(record["session"], artifact.payload, artifact.payload)
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='creative_v2_runs'").fetchone()
    state = (
        db.execute(
            "SELECT payload FROM creative_v2_runs WHERE run_id=?", (artifact.run_id,)
        ).fetchone()
        if exists
        else None
    )
    if state:
        check_reference_snapshot(
            store.path.parent.parent, json.loads(state[0]).get("references", [])
        )
    return task


def continue_original_task_preview(
    store: WorkflowStore,
    project: str,
    preview_id: str,
    *,
    expected_seq: int,
) -> dict:
    with store.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS creative_original_preview_forks(
            command_key TEXT PRIMARY KEY, project TEXT NOT NULL, payload TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise KeyError("原试玩不存在")
        source = json.loads(row[0])
        if source["session"]["action_seq"] != expected_seq:
            raise ValueError("原试玩位置已改变")
        task = _verify_original_task_preview(db, store, project, source)
        command = content_hash([preview_id, content_hash(source)])
        saved = db.execute(
            "SELECT payload FROM creative_original_preview_forks WHERE command_key=? AND project=?",
            (command, project),
        ).fetchone()
        if saved:
            prior_fork = _decode(saved[0], "migration_hash")
            existing = json.loads(
                db.execute(
                    "SELECT payload FROM previews WHERE id=?", (prior_fork["preview_id"],)
                ).fetchone()[0]
            )
            return _task_preview_result(existing)
        domain = {**source["session"], "id": new_id(), "applied_actions": {}}
        record = {
            **source,
            "session": domain,
            "parent_preview_id": preview_id,
            "version_mode": "fixed_historical",
            "frozen_base": {
                "base_hash": task.base_hash,
                "base_version": task.base_version,
                "world_id": task.request.world_id,
            },
        }
        migration = _sealed(
            {
                "id": new_id(),
                "source_preview_id": preview_id,
                "source_preview_hash": content_hash(source),
                "preview_id": domain["id"],
                "source_artifact_id": source["artifact_id"],
                "frozen_base": record["frozen_base"],
                "policy": "fixed_historical_artifact_v1",
                "created_at": now(),
            },
            "migration_hash",
        )
        record["snapshot_migration"] = migration
        db.execute(
            "INSERT INTO previews VALUES(?,?)",
            (domain["id"], json.dumps(record, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO creative_original_preview_forks VALUES(?,?,?)",
            (
                command,
                project,
                json.dumps(
                    _sealed({"preview_id": domain["id"], "migration": migration}, "migration_hash"),
                    ensure_ascii=False,
                ),
            ),
        )
        return _task_preview_result(record)


def step_original_task_preview(
    store: WorkflowStore,
    project: str,
    preview_id: str,
    request: Any,
) -> dict:
    from ..logic.stepper import advance_preview

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise KeyError("旧版隔离试玩不存在")
        record = json.loads(row[0])
        if record.get("version_mode") != "fixed_historical":
            raise ValueError("此接口只推进明确固定版本的旧版隔离试玩")
        _verify_original_task_preview(db, store, project, record)
        domain = advance_preview(record["session"], **request.model_dump())
        record["session"] = domain.model_dump(mode="json")
        db.execute(
            "UPDATE previews SET payload=? WHERE id=?",
            (
                json.dumps(record, ensure_ascii=False),
                preview_id,
            ),
        )
        return _task_preview_result(record)
