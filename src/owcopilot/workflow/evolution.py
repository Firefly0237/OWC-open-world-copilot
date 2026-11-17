"""Evidence-grounded skill versions and bounded evolution, separate from legacy induction.

ACE-style item deltas retain their sources. Technical execution, trial permission,
author acceptance and improvement are independent facts. This module never calls a
model: a production proposal must come through the caller's budgeted Harness.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from ..content.hash import content_hash
from ..retrieval.text_match import lexical_score
from .budget_policy import active_policy
from .models import new_id, now
from .store import WorkflowStore

GraphValidator = Callable[[dict[str, Any]], Any]
GraphRewriter = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
ExperienceKind = Literal[
    "success", "technical_failure", "author_feedback", "provider_failure", "hypothesis"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_Model):
    kind: Literal["run", "feedback", "artifact", "development_input", "method"]
    id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GuidanceEntry(_Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    text: str = Field(min_length=1, max_length=8000)
    operation: str = Field(default="", max_length=100)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=30)
    evidence_kind: Literal["observation", "author_preference", "hypothesis"] = "hypothesis"


class MachineRequirement(_Model):
    key: str = Field(min_length=1, max_length=120)
    value: StrictBool | StrictInt | StrictStr
    confirmed: bool = Field(default=False, strict=True)
    source_ref: SourceRef | None = None


class SkillConditions(_Model):
    required_resources: list[str] = Field(default_factory=list, max_length=100)
    required_capabilities: list[str] = Field(default_factory=list, max_length=100)
    requirements: list[MachineRequirement] = Field(default_factory=list, max_length=50)
    free_text: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_conditions(self) -> SkillConditions:
        for values in (
            self.required_resources,
            self.required_capabilities,
            [item.key for item in self.requirements],
        ):
            if len(values) != len(set(values)):
                raise ValueError("技能适用条件不能重复")
        return self


class SkillDelta(_Model):
    rationale: str = Field(min_length=1, max_length=8000)
    target_problem: str = Field(min_length=1, max_length=4000)
    graph_changes: dict[str, Any] | None = None
    upsert_entries: list[GuidanceEntry] = Field(default_factory=list, max_length=50)
    remove_entry_ids: list[str] = Field(default_factory=list, max_length=50)
    conditions: SkillConditions | None = None
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def substantive_delta(self) -> SkillDelta:
        ids = [item.id for item in self.upsert_entries]
        if len(ids) != len(set(ids)) or len(self.remove_entry_ids) != len(
            set(self.remove_entry_ids)
        ):
            raise ValueError("同一条目只能在一次delta中操作一次")
        if set(ids) & set(self.remove_entry_ids):
            raise ValueError("同一条目不能同时修改和删除")
        if not (
            self.graph_changes
            or self.upsert_entries
            or self.remove_entry_ids
            or self.conditions is not None
            or self.description is not None
        ):
            raise ValueError("delta必须声明实际变化")
        return self


class ExecutionEvidence(_Model):
    """Host-produced execution observations; these do not establish author satisfaction."""

    run_id: str = Field(min_length=1)
    graph_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["completed", "completed_with_skips", "failed", "cancelled"]
    consumed_artifact_hashes: list[str] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    checks: dict[str, StrictBool] = Field(default_factory=dict)
    source_kind: Literal["production_run", "controlled_development"] = "production_run"


def _sealed(record: dict[str, Any], field: str) -> dict[str, Any]:
    return {**record, field: content_hash(record)}


def _decode(raw: str, field: str) -> dict[str, Any]:
    record = json.loads(raw)
    if record.get(field) != content_hash({k: v for k, v in record.items() if k != field}):
        raise ValueError("技能记录hash校验失败")
    return record


def assess_conditions(
    conditions: SkillConditions,
    *,
    resources: set[str] | None,
    capabilities: set[str] | None,
    requirements: Mapping[str, Any] | None = None,
    confirmed_requirement_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Only explicit machine requirements can conflict; prose remains unknown."""
    conflicts: list[str] = []
    unknown: list[str] = []
    for label, needed, available in (
        ("resource", conditions.required_resources, resources),
        ("capability", conditions.required_capabilities, capabilities),
    ):
        if needed and available is None:
            unknown.append(label + ":unobserved")
        elif available is not None:
            conflicts.extend(label + ":" + value for value in needed if value not in available)
    current = requirements or {}
    confirmed = confirmed_requirement_keys or set()
    for rule in conditions.requirements:
        if not rule.confirmed or rule.key not in confirmed or rule.key not in current:
            unknown.append("requirement:" + rule.key)
        elif type(current[rule.key]) is not type(rule.value) or current[rule.key] != rule.value:
            conflicts.append("requirement:" + rule.key)
    unknown.extend("free_text:" + value for value in conditions.free_text)
    return {
        "status": "conflict" if conflicts else "unknown" if unknown else "compatible",
        "compatible": not conflicts and not unknown,
        "conflicts": conflicts,
        "unknown": unknown,
        "scope": "declared_machine_conditions_only_not_semantic_or_utility_proof",
    }


class EvolutionStore:
    """Append-only versions/evidence plus a CAS-protected family selection pointer."""

    def __init__(self, workflow: WorkflowStore, graph_validator: GraphValidator | None = None):
        self.workflow = workflow
        self.graph_validator = graph_validator
        with workflow.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evolution_families(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_versions(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, family_id TEXT NOT NULL,
                    version INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(family_id,version));
                CREATE TABLE IF NOT EXISTS evolution_states(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
                    version_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evolution_states_version
                    ON evolution_states(version_id,seq);
                CREATE TABLE IF NOT EXISTS evolution_experiences(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, family_id TEXT NOT NULL,
                    event_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_episodes(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, family_id TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_episode_results(
                    episode_id TEXT PRIMARY KEY, project TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_episode_claims(
                    episode_id TEXT PRIMARY KEY, project TEXT NOT NULL, claimed_at TEXT NOT NULL);
            """)

    @staticmethod
    def _family(db: sqlite3.Connection, project: str, family_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM evolution_families WHERE id=? AND project=?", (family_id, project)
        ).fetchone()
        if row is None:
            raise KeyError("技能家族不属于当前项目或不存在")
        family = _decode(row["payload"], "family_hash")
        if family["id"] != row["id"] or family["project"] != row["project"]:
            raise ValueError("技能家族索引与内容不一致")
        return family

    @staticmethod
    def _version(db: sqlite3.Connection, project: str, version_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM evolution_versions WHERE id=? AND project=?", (version_id, project)
        ).fetchone()
        if row is None:
            raise KeyError("技能版本不属于当前项目或不存在")
        record = _decode(row["payload"], "version_hash")
        if (record["id"], record["project"], record["family_id"], record["version"]) != (
            row["id"],
            row["project"],
            row["family_id"],
            row["version"],
        ) or content_hash(record["graph"]) != record["graph_hash"]:
            raise ValueError("技能版本索引、图或hash不一致")
        return record

    @staticmethod
    def _state(db: sqlite3.Connection, project: str, version_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT payload FROM evolution_states WHERE project=? AND version_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (project, version_id),
        ).fetchone()
        if row is None:
            raise ValueError("技能版本缺少技术与作者状态")
        return _decode(row["payload"], "state_hash")

    @staticmethod
    def _check_token(family: dict[str, Any], expected: str) -> None:
        if family["family_hash"] != expected:
            raise ValueError("技能家族已改变，请读取最新版本")

    @staticmethod
    def _write_family(db: sqlite3.Connection, family: dict[str, Any], **changes: Any):
        payload = {k: v for k, v in family.items() if k != "family_hash"}
        payload.update(changes)
        payload["revision"] += 1
        result = _sealed(payload, "family_hash")
        db.execute(
            "UPDATE evolution_families SET payload=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), family["id"]),
        )
        return result

    @classmethod
    def _append_state(
        cls,
        db: sqlite3.Connection,
        version: dict[str, Any],
        *,
        changes: dict[str, Any],
        action: str,
        evidence: dict[str, Any],
        initial: bool = False,
    ) -> dict[str, Any]:
        old = {} if initial else cls._state(db, version["project"], version["id"])
        axes = {
            "technical_status": old.get("technical_status", "schema_validated"),
            "trial_status": old.get("trial_status", "unlicensed"),
            "author_acceptance": old.get("author_acceptance", "unknown"),
            "improvement_status": old.get("improvement_status", "unassessed"),
        }
        axes.update(changes)
        state = _sealed(
            {
                "id": new_id(),
                "version_id": version["id"],
                "project": version["project"],
                **axes,
                "previous_state_hash": old.get("state_hash"),
                "action": action,
                "evidence": evidence,
                "created_at": now(),
            },
            "state_hash",
        )
        db.execute(
            "INSERT INTO evolution_states(project,version_id,payload) VALUES(?,?,?)",
            (version["project"], version["id"], json.dumps(state, ensure_ascii=False)),
        )
        return state

    def create_family(self, project: str, name: str, description: str = "") -> dict[str, Any]:
        if not project.strip() or not name.strip():
            raise ValueError("技能项目和名称不能为空")
        family = _sealed(
            {
                "id": new_id(),
                "project": project,
                "name": name.strip(),
                "description": description,
                "revision": 1,
                "next_version": 1,
                "default_version_id": None,
                "disabled": False,
                "created_at": now(),
            },
            "family_hash",
        )
        with self.workflow.connection() as db:
            db.execute(
                "INSERT INTO evolution_families VALUES(?,?,?)",
                (family["id"], project, json.dumps(family, ensure_ascii=False)),
            )
        return family

    def get_family(self, project: str, family_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            return self._family(db, project, family_id)

    def families(self, project: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            return [
                self._family(db, project, row["id"])
                for row in db.execute(
                    "SELECT id FROM evolution_families WHERE project=? ORDER BY rowid", (project,)
                ).fetchall()
            ]

    def get_version(self, project: str, version_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            version = self._version(db, project, version_id)
            return {**version, "state": self._state(db, project, version_id)}

    def versions(self, project: str, family_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            self._family(db, project, family_id)
            return [
                {
                    **self._version(db, project, row["id"]),
                    "state": self._state(db, project, row["id"]),
                }
                for row in db.execute(
                    "SELECT id FROM evolution_versions WHERE project=? AND family_id=? "
                    "ORDER BY version",
                    (project, family_id),
                ).fetchall()
            ]

    def _validate_graph(self, graph: dict[str, Any]) -> tuple[dict[str, Any], str, str | None]:
        from .graph_v2 import GraphSpecV2

        normalized = GraphSpecV2.model_validate(graph).model_dump(mode="json")
        if self.graph_validator is None:
            return normalized, "schema_validated", None
        compiled = self.graph_validator(normalized)
        compiled_hash = (
            compiled.get("graph_hash")
            if isinstance(compiled, dict)
            else getattr(compiled, "graph_hash", None)
        )
        if not isinstance(compiled_hash, str) or len(compiled_hash) != 64:
            raise ValueError("图编译器必须返回实际端口和角色合同的graph_hash")
        return normalized, "compiled", compiled_hash

    def add_version(
        self,
        project: str,
        family_id: str,
        *,
        graph: dict[str, Any],
        expected_family_hash: str,
        entries: list[GuidanceEntry] | None = None,
        conditions: SkillConditions | None = None,
        source_refs: list[SourceRef] | None = None,
        parent_version_id: str | None = None,
        description: str = "",
        delta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph, technical, compiled_hash = self._validate_graph(graph)
        entries = [GuidanceEntry.model_validate(item) for item in entries or []]
        if len({item.id for item in entries}) != len(entries):
            raise ValueError("技能指导条目ID重复")
        conditions = SkillConditions.model_validate(conditions or SkillConditions())
        sources = [
            SourceRef.model_validate(item).model_dump(mode="json") for item in source_refs or []
        ]
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            family = self._family(db, project, family_id)
            self._check_token(family, expected_family_hash)
            parent = self._version(db, project, parent_version_id) if parent_version_id else None
            if parent is not None and parent["family_id"] != family_id:
                raise ValueError("父版本必须来自同一技能家族")
            if family["next_version"] > 1 and parent is None:
                raise ValueError("后续版本必须显式保留父版本")
            material = {
                "graph": graph,
                "entries": [item.model_dump(mode="json") for item in entries],
                "conditions": conditions.model_dump(mode="json"),
                "description": description,
            }
            if parent and all(material[key] == parent[key] for key in material):
                raise ValueError("技能新版本必须有实际变化")
            version = _sealed(
                {
                    "id": new_id(),
                    "project": project,
                    "family_id": family_id,
                    "version": family["next_version"],
                    "parent_version_id": parent_version_id,
                    **material,
                    "graph_hash": content_hash(graph),
                    "source_refs": sources,
                    "execution_graph_hash": compiled_hash,
                    "delta": delta,
                    "created_at": now(),
                },
                "version_hash",
            )
            db.execute(
                "INSERT INTO evolution_versions VALUES(?,?,?,?,?)",
                (
                    version["id"],
                    project,
                    family_id,
                    version["version"],
                    json.dumps(version, ensure_ascii=False),
                ),
            )
            self._append_state(
                db,
                version,
                changes={"technical_status": technical},
                action="version_created",
                evidence={},
                initial=True,
            )
            self._write_family(db, family, next_version=family["next_version"] + 1)
        return self.get_version(project, version["id"])

    def apply_delta(
        self,
        project: str,
        parent_version_id: str,
        delta: SkillDelta,
        *,
        expected_family_hash: str,
        graph_rewriter: GraphRewriter | None = None,
        source_refs: list[SourceRef] | None = None,
    ) -> dict[str, Any]:
        delta = SkillDelta.model_validate(delta)
        parent = self.get_version(project, parent_version_id)
        entries = {item["id"]: GuidanceEntry.model_validate(item) for item in parent["entries"]}
        for entry_id in delta.remove_entry_ids:
            if entry_id not in entries:
                raise ValueError("不能删除不存在的技能条目")
            del entries[entry_id]
        entries.update({item.id: item for item in delta.upsert_entries})
        graph = parent["graph"]
        if delta.graph_changes is not None:
            if graph_rewriter is None:
                raise ValueError("结构delta需要注册受约束图重写器")
            graph = graph_rewriter(graph, delta.graph_changes)
        return self.add_version(
            project,
            parent["family_id"],
            graph=graph,
            entries=list(entries.values()),
            conditions=delta.conditions or SkillConditions.model_validate(parent["conditions"]),
            description=delta.description
            if delta.description is not None
            else parent["description"],
            parent_version_id=parent_version_id,
            expected_family_hash=expected_family_hash,
            source_refs=source_refs,
            delta=delta.model_dump(mode="json"),
        )

    def record_evaluation(
        self,
        project: str,
        version_id: str,
        evidence: ExecutionEvidence,
        *,
        improvement: Literal[
            "unassessed", "target_resolved", "tradeoff", "regressed"
        ] = "unassessed",
        comparison_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence = ExecutionEvidence.model_validate(evidence)
        if improvement != "unassessed" and not comparison_evidence:
            raise ValueError("相对改善判断需要独立的可比证据")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = self._version(db, project, version_id)
            if evidence.graph_hash != version.get("execution_graph_hash"):
                raise ValueError("执行证据不是该技能图版本")
            state = self._state(db, project, version_id)
            passed = (
                state["technical_status"] in {"compiled", "passed", "failed"}
                and evidence.status == "completed"
                and not evidence.failed_nodes
                and bool(evidence.consumed_artifact_hashes)
                and all(evidence.checks.values())
            )
            return self._append_state(
                db,
                version,
                changes={
                    "technical_status": "passed" if passed else "failed",
                    "trial_status": "completed",
                    "improvement_status": improvement,
                },
                action="host_execution_evaluation",
                evidence={
                    "execution": evidence.model_dump(mode="json"),
                    "comparison": comparison_evidence,
                    "scope": "observed_execution_not_generalization_or_author_satisfaction",
                },
            )

    def authorize_trial(
        self,
        project: str,
        version_id: str,
        *,
        expected_family_hash: str,
        allowed: bool,
        authority: Literal[
            "explicit_author_action", "preauthorized_policy", "development_operator"
        ],
        note: str = "",
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = self._version(db, project, version_id)
            family = self._family(db, project, version["family_id"])
            self._check_token(family, expected_family_hash)
            if authority not in {
                "explicit_author_action", "preauthorized_policy", "development_operator"
            }:
                raise ValueError("试用需要明确作者操作或已记录的预授权")
            state = self._state(db, project, version_id)
            if allowed and (
                state["technical_status"] not in {"compiled", "passed"}
                or state["author_acceptance"] == "rejected"
                or family["disabled"]
            ):
                raise ValueError("未编译、被拒绝或已停用的方法不能试用")
            return self._append_state(
                db,
                version,
                changes={"trial_status": "allowed" if allowed else "unlicensed"},
                action=authority,
                evidence={"note": note},
            )

    def decide_version(
        self,
        project: str,
        version_id: str,
        *,
        expected_family_hash: str,
        decision: Literal["accepted", "rejected"],
        make_default: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = self._version(db, project, version_id)
            family = self._family(db, project, version["family_id"])
            self._check_token(family, expected_family_hash)
            state = self._state(db, project, version_id)
            if decision not in {"accepted", "rejected"}:
                raise ValueError("作者决定必须明确接受或拒绝")
            if decision == "accepted" and state["technical_status"] not in {"compiled", "passed"}:
                raise ValueError("未通过编译检查的方法不能接受")
            if make_default and (decision != "accepted" or state["technical_status"] != "passed"):
                raise ValueError("默认方法需要实际通过执行并被作者明确接受")
            result = self._append_state(
                db,
                version,
                changes={"author_acceptance": decision},
                action="explicit_author_decision",
                evidence={"note": note},
            )
            default = family["default_version_id"]
            if make_default:
                default = version_id
            elif decision == "rejected" and default == version_id:
                default = None
            self._write_family(db, family, default_version_id=default)
            return {"state": result, "default_version_id": default, "changes_content": False}

    def set_default(
        self,
        project: str,
        family_id: str,
        version_id: str,
        *,
        expected_family_hash: str,
        action: Literal["select", "rollback"] = "select",
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            family = self._family(db, project, family_id)
            self._check_token(family, expected_family_hash)
            version = self._version(db, project, version_id)
            state = self._state(db, project, version_id)
            if version["family_id"] != family_id:
                raise ValueError("不能采用其他技能家族的版本")
            if state["technical_status"] != "passed" or state["author_acceptance"] != "accepted":
                raise ValueError("默认／退回目标须有执行证据与明确作者接受")
            if family["disabled"]:
                raise ValueError("技能家族已停用")
            result = self._write_family(db, family, default_version_id=version_id)
            self._append_state(
                db,
                version,
                changes={},
                action="explicit_" + action,
                evidence={"previous_default": family["default_version_id"]},
            )
            return result

    def set_disabled(
        self,
        project: str,
        family_id: str,
        *,
        expected_family_hash: str,
        disabled: bool,
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            family = self._family(db, project, family_id)
            self._check_token(family, expected_family_hash)
            return self._write_family(db, family, disabled=disabled)

    def record_experience(
        self,
        project: str,
        family_id: str,
        *,
        kind: ExperienceKind,
        source_ref: SourceRef,
        payload: dict[str, Any],
        problem_key: str,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {
            "success",
            "technical_failure",
            "author_feedback",
            "provider_failure",
            "hypothesis",
        }:
            raise ValueError("未知经验类型")
        source = SourceRef.model_validate(source_ref).model_dump(mode="json")
        if not problem_key.strip():
            raise ValueError("经验必须保留稳定问题身份")
        event_key = content_hash([project, family_id, kind, source, problem_key])
        material = {
            "project": project,
            "family_id": family_id,
            "version_id": version_id,
            "kind": kind,
            "source_ref": source,
            "payload": payload,
            "problem_key": problem_key,
            "creative_evolution_eligible": kind != "provider_failure",
            "author_acceptance": "unknown",
        }
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._family(db, project, family_id)
            if version_id and self._version(db, project, version_id)["family_id"] != family_id:
                raise ValueError("经验所指版本不属于技能家族")
            existing = db.execute(
                "SELECT payload FROM evolution_experiences WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing:
                record = _decode(existing[0], "experience_hash")
                if any(record[key] != value for key, value in material.items()):
                    raise ValueError("同一来源经验不能覆盖为不同内容")
                return record
            record = _sealed({"id": new_id(), **material, "created_at": now()}, "experience_hash")
            db.execute(
                "INSERT INTO evolution_experiences VALUES(?,?,?,?,?)",
                (
                    record["id"],
                    project,
                    family_id,
                    event_key,
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            return record

    def experiences(self, project: str, family_id: str) -> list[dict[str, Any]]:
        with self.workflow.connection() as db:
            self._family(db, project, family_id)
            return [
                _decode(row[0], "experience_hash")
                for row in db.execute(
                    "SELECT payload FROM evolution_experiences WHERE project=? AND family_id=? "
                    "ORDER BY rowid",
                    (project, family_id),
                ).fetchall()
            ]

    def begin_episode(
        self,
        project: str,
        family_id: str,
        experience_id: str,
        problem_key: str,
        *,
        parent_version_id: str,
    ) -> dict[str, Any]:
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._family(db, project, family_id)
            parent = self._version(db, project, parent_version_id)
            row = db.execute(
                "SELECT payload FROM evolution_experiences "
                "WHERE id=? AND project=? AND family_id=?",
                (experience_id, project, family_id),
            ).fetchone()
            if row is None or parent["family_id"] != family_id:
                raise KeyError("演化来源或父版本不属于当前技能")
            experience = _decode(row[0], "experience_hash")
            if not experience["creative_evolution_eligible"]:
                raise ValueError("供应商／网络故障只属于运行诊断，不触发创作技能改写")
            if problem_key != experience["problem_key"]:
                raise ValueError("演化不能改变来源的问题身份")
            dedupe = content_hash([project, family_id, experience_id, problem_key])
            existing = db.execute(
                "SELECT payload FROM evolution_episodes WHERE dedupe_key=?", (dedupe,)
            ).fetchone()
            if existing:
                record = _decode(existing[0], "episode_hash")
                if record["parent_version_id"] != parent_version_id:
                    raise ValueError("同一演化来源不能另选父版本重启")
                return record
            policy = active_policy(db)
            record = _sealed(
                {
                    "id": new_id(),
                    "project": project,
                    "family_id": family_id,
                    "experience_id": experience_id,
                    "experience_hash": experience["experience_hash"],
                    "parent_version_id": parent_version_id,
                    "parent_version_hash": parent["version_hash"],
                    "problem_key": problem_key,
                    "budget_owner": "evolution:" + content_hash([project, family_id, problem_key]),
                    "max_model_dispatches": policy["policy"]["max_owner_dispatches"],
                    "budget_policy": policy,
                    "candidate_limit": 1,
                    "created_at": now(),
                },
                "episode_hash",
            )
            db.execute(
                "INSERT INTO evolution_episodes VALUES(?,?,?,?,?)",
                (record["id"], project, family_id, dedupe, json.dumps(record, ensure_ascii=False)),
            )
            return record

    def get_episode(self, project: str, episode_id: str) -> dict[str, Any]:
        with self.workflow.connection() as db:
            row = db.execute(
                "SELECT payload FROM evolution_episodes WHERE id=? AND project=?",
                (episode_id, project),
            ).fetchone()
            if row is None:
                raise KeyError("演化过程不属于当前项目或不存在")
            episode = _decode(row[0], "episode_hash")
            result = db.execute(
                "SELECT payload FROM evolution_episode_results WHERE episode_id=? AND project=?",
                (episode_id, project),
            ).fetchone()
            return {**episode, "result": _decode(result[0], "result_hash") if result else None}

    def claim_episode(self, project: str, episode_id: str) -> bool:
        """Reserve one proposal operation; a crashed owner remains visibly interrupted."""
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM evolution_episodes WHERE id=? AND project=?", (episode_id, project)
            ).fetchone()
            if row is None:
                raise KeyError("演化过程不属于当前项目或不存在")
            return (
                db.execute(
                    "INSERT OR IGNORE INTO evolution_episode_claims VALUES(?,?,?)",
                    (episode_id, project, now()),
                ).rowcount
                == 1
            )

    def finish_episode(
        self,
        project: str,
        episode_id: str,
        *,
        status: Literal["candidate", "blocked", "failed", "no_change", "insufficient_evidence"],
        candidate_version_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"candidate", "blocked", "failed", "no_change", "insufficient_evidence"}:
            raise ValueError("未知演化终态")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM evolution_episodes WHERE id=? AND project=?",
                (episode_id, project),
            ).fetchone()
            if row is None:
                raise KeyError("演化过程不存在")
            episode = _decode(row[0], "episode_hash")
            if candidate_version_id:
                candidate = self._version(db, project, candidate_version_id)
                if (
                    candidate["family_id"] != episode["family_id"]
                    or candidate["parent_version_id"] != episode["parent_version_id"]
                ):
                    raise ValueError("候选不是该演化父版本的后继")
            if (status == "candidate") != bool(candidate_version_id):
                raise ValueError("候选终态必须且只能关联一个实际版本")
            material = {
                "episode_id": episode_id,
                "project": project,
                "status": status,
                "candidate_version_id": candidate_version_id,
                "evidence": evidence or {},
            }
            old = db.execute(
                "SELECT payload FROM evolution_episode_results WHERE episode_id=?", (episode_id,)
            ).fetchone()
            if old:
                result = _decode(old[0], "result_hash")
                if any(result[key] != value for key, value in material.items()):
                    raise ValueError("一次演化只能有一个不可覆盖的结果")
                return result
            result = _sealed({**material, "created_at": now()}, "result_hash")
            db.execute(
                "INSERT INTO evolution_episode_results VALUES(?,?,?)",
                (episode_id, project, json.dumps(result, ensure_ascii=False)),
            )
            return result

    def match(
        self,
        project: str,
        query: str,
        *,
        resources: set[str] | None,
        capabilities: set[str] | None,
        requirements: Mapping[str, Any] | None = None,
        confirmed_requirement_keys: set[str] | None = None,
        include_trials: bool = False,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        results = []
        for family in self.families(project):
            if family["disabled"]:
                continue
            for version in self.versions(project, family["id"]):
                state = version["state"]
                selected = family["default_version_id"] == version["id"]
                trial = include_trials and state["trial_status"] == "allowed"
                if not (selected or trial) or state["author_acceptance"] == "rejected":
                    continue
                if state["technical_status"] not in {"compiled", "passed"}:
                    continue
                assessment = assess_conditions(
                    SkillConditions.model_validate(version["conditions"]),
                    resources=resources,
                    capabilities=capabilities,
                    requirements=requirements,
                    confirmed_requirement_keys=confirmed_requirement_keys,
                )
                if assessment["status"] == "conflict":
                    continue
                score = lexical_score(
                    query,
                    [
                        family["name"],
                        family["description"],
                        version["description"],
                        *(item["text"] for item in version["entries"]),
                    ],
                )
                results.append(
                    {
                        "family_id": family["id"],
                        "version_id": version["id"],
                        "version": version,
                        "assessment": assessment,
                        "score": score,
                        "selection": "default" if selected else "authorized_trial",
                        "retrieval": "lexical_and_declared_capabilities_not_graph_nearest_neighbor",
                        "eligible_to_execute": assessment["compatible"],
                    }
                )
        return sorted(results, key=lambda item: (not item["eligible_to_execute"], -item["score"]))[
            : max(0, min(limit, 20))
        ]


def run_evolution_episode(
    store: EvolutionStore,
    project: str,
    episode_id: str,
    *,
    budgeted_propose: Callable[[dict[str, Any]], SkillDelta],
    graph_rewriter: GraphRewriter | None = None,
) -> dict[str, Any]:
    """One proposal, no retries or automatic acceptance; caller owns Harness dispatch.

    ``budgeted_propose`` is a trusted application callback, not a client-supplied
    function. It must use the episode's stable budget_owner and persisted grant
    for every reflector/curator/continuation dispatch. This pure workflow
    neither grants another budget nor re-executes candidate production nodes.
    """
    episode = store.get_episode(project, episode_id)
    if episode["result"] is not None:
        return episode
    if not store.claim_episode(project, episode_id):
        return {
            **store.get_episode(project, episode_id),
            "status": "in_progress_or_interrupted",
            "new_proposal_dispatched": False,
        }
    parent = store.get_version(project, episode["parent_version_id"])
    family = store.get_family(project, episode["family_id"])
    experience = next(
        row
        for row in store.experiences(project, family["id"])
        if row["id"] == episode["experience_id"]
    )
    context = {
        "episode": {key: value for key, value in episode.items() if key != "result"},
        "parent": parent,
        "experience": experience,
        "delta_schema": SkillDelta.model_json_schema(),
        "limits": "one candidate; no author acceptance; shared dispatch cap=3",
    }
    try:
        delta = SkillDelta.model_validate(budgeted_propose(context))
        candidate = store.apply_delta(
            project,
            parent["id"],
            delta,
            expected_family_hash=family["family_hash"],
            graph_rewriter=graph_rewriter,
            source_refs=[SourceRef.model_validate(experience["source_ref"])],
        )
    except Exception as exc:
        store.finish_episode(
            project,
            episode_id,
            status="failed",
            evidence={
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "candidate_execution": "not_performed",
                "author_acceptance": "unknown",
            },
        )
        return store.get_episode(project, episode_id)
    store.finish_episode(
        project,
        episode_id,
        status="candidate",
        candidate_version_id=candidate["id"],
        evidence={"candidate_execution": "not_performed", "author_acceptance": "unknown"},
    )
    return store.get_episode(project, episode_id)
