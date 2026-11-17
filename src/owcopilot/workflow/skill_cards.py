"""Versioned procedural skills, explicit selection, and source-bound execution uses."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from .evolution import SourceRef
from .evolution_v2 import ParameterSlot, ReusableEntry
from .graph_v2 import GraphSpecV2
from .models import new_id, now

_PRODUCTION_ROLES = {
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
}


class SkillStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=1800)
    operation: str = ""
    slots: list[ParameterSlot] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid(self):
        if self.operation and self.operation not in _PRODUCTION_ROLES:
            raise ValueError("Skill只提供制作指导，不能修改系统检查或注册新权限")
        self.entry("validation", SourceRef(kind="method", id="schema", content_hash="0" * 64))
        return self

    def entry(self, prefix: str, source: SourceRef):
        namespace = content_hash([prefix, self.id])[:16]
        names = {slot.name: "slot_" + namespace + "_" + str(i) for i, slot in enumerate(self.slots)}
        text = re.sub(
            r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}",
            lambda match: "{{" + names.get(match.group(1), match.group(1)) + "}}",
            self.instruction,
        )
        return ReusableEntry(
            id="step_" + namespace,
            text=text,
            operation=self.operation,
            slots=[slot.model_copy(update={"name": names[slot.name]}) for slot in self.slots],
            evidence_kind="hypothesis",
            support_refs=[source],
        )


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=500)
    steps: list[SkillStep] = Field(min_length=1, max_length=8)
    applies_to: list[str] = Field(min_length=1, max_length=5)
    does_not_apply_to: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def bounded(self):
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("Skill步骤ID重复")
        if any(
            not text.strip() or len(text) > 500 for text in self.applies_to + self.does_not_apply_to
        ):
            raise ValueError("适用边界需简短且明确")
        if sum(len(step.instruction) for step in self.steps) > 6000:
            raise ValueError("Skill过长，请拆分为有明确用途的方法")
        return self


class SkillSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    card_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    version_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def _seal(record, key):
    return {**record, key: content_hash(record)}


def _read(raw, key):
    record = json.loads(raw)
    if record.get(key) != content_hash({k: v for k, v in record.items() if k != key}):
        raise ValueError("Skill记录完整性校验失败")
    return record


def definition_fingerprint(definition: SkillDefinition):
    material = definition.model_dump(mode="json")
    for step in material["steps"]:
        step.pop("id")
        for slot in step["slots"]:
            slot.pop("source_value", None)
    return content_hash(material)


class SkillCards:
    def __init__(self, workflow):
        self.workflow = workflow
        with workflow.connection() as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS skill_cards("
                "id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS skill_card_versions(id TEXT PRIMARY KEY,"
                "card_id TEXT NOT NULL,project TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS skill_card_events("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "card_id TEXT NOT NULL,project TEXT NOT NULL,payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS skill_card_uses(id TEXT PRIMARY KEY,"
                "run_id TEXT NOT NULL,project TEXT NOT NULL,payload TEXT NOT NULL)",
            ):
                db.execute(statement)

    @staticmethod
    def _head(db, project, card_id):
        row = db.execute(
            "SELECT payload FROM skill_cards WHERE id=? AND project=?", (card_id, project)
        ).fetchone()
        if row is None:
            raise KeyError("Skill不属于当前工作区或不存在")
        return _read(row[0], "card_hash")

    @staticmethod
    def _version(db, project, version_id):
        row = db.execute(
            "SELECT payload FROM skill_card_versions WHERE id=? AND project=?",
            (version_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("Skill版本不存在")
        return _read(row[0], "version_hash")

    @staticmethod
    def _event(db, head, kind, payload):
        record = _seal(
            {"card_id": head["id"], "kind": kind, "payload": payload, "created_at": now()},
            "event_hash",
        )
        db.execute(
            "INSERT INTO skill_card_events(card_id,project,payload) VALUES(?,?,?)",
            (head["id"], head["project"], json.dumps(record, ensure_ascii=False)),
        )

    @staticmethod
    def _write_head(db, head, **changes):
        plain = {k: v for k, v in head.items() if k != "card_hash"}
        updated = _seal({**plain, **changes, "generation": head["generation"] + 1}, "card_hash")
        db.execute(
            "UPDATE skill_cards SET payload=? WHERE id=?",
            (json.dumps(updated, ensure_ascii=False), head["id"]),
        )
        return updated

    def add_candidate(
        self,
        project,
        definition,
        source,
        *,
        card_id=None,
        expected_card_hash=None,
        parent_version_id=None,
        changes=None,
    ):
        definition = SkillDefinition.model_validate(definition)
        fingerprint = definition_fingerprint(definition)
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if card_id:
                head = self._head(db, project, card_id)
                if head["card_hash"] != expected_card_hash:
                    raise ValueError("Skill已变化，请重新查看后更新")
                parent = self._version(db, project, parent_version_id)
                if parent["card_id"] != card_id:
                    raise ValueError("更新不能引用其他Skill的父版本")
                if parent["fingerprint"] == fingerprint:
                    self._event(
                        db,
                        head,
                        "no_change",
                        {"parent_version_id": parent_version_id, "source": source},
                    )
                    return self._detail(db, head), False
            else:
                # Exact duplicate content reuses a family; similarity alone never merges meanings.
                for row in db.execute(
                    "SELECT payload FROM skill_card_versions WHERE project=?", (project,)
                ):
                    existing = _read(row[0], "version_hash")
                    if existing["fingerprint"] == fingerprint:
                        head = self._head(db, project, existing["card_id"])
                        self._event(
                            db,
                            head,
                            "duplicate_evidence",
                            {"version_id": existing["id"], "source": source},
                        )
                        return self._detail(db, head), False
                card_id = new_id()
                head = _seal(
                    {
                        "id": card_id,
                        "project": project,
                        "generation": 0,
                        "current_version_id": None,
                        "active_version_id": None,
                        "enabled": True,
                        "created_at": now(),
                    },
                    "card_hash",
                )
                db.execute(
                    "INSERT INTO skill_cards VALUES(?,?,?)",
                    (card_id, project, json.dumps(head, ensure_ascii=False)),
                )
            number = (
                db.execute(
                    "SELECT count(*) FROM skill_card_versions WHERE card_id=?", (card_id,)
                ).fetchone()[0]
                + 1
            )
            record = _seal(
                {
                    "id": new_id(),
                    "card_id": card_id,
                    "project": project,
                    "version": number,
                    "parent_version_id": parent_version_id,
                    "definition": definition.model_dump(mode="json"),
                    "fingerprint": fingerprint,
                    "source": source,
                    "changes": changes or [],
                    "created_at": now(),
                },
                "version_hash",
            )
            db.execute(
                "INSERT INTO skill_card_versions VALUES(?,?,?,?)",
                (record["id"], card_id, project, json.dumps(record, ensure_ascii=False)),
            )
            head = self._write_head(db, head, current_version_id=record["id"])
            self._event(
                db, head, "candidate_created", {"version_id": record["id"], "source": source}
            )
            return self._detail(db, head), True

    def _detail(self, db, head):
        events = [
            _read(row[0], "event_hash")
            for row in db.execute(
                "SELECT payload FROM skill_card_events WHERE card_id=? ORDER BY seq", (head["id"],)
            )
        ]
        versions = []
        for row in db.execute(
            "SELECT payload FROM skill_card_versions WHERE card_id=? ORDER BY rowid", (head["id"],)
        ):
            version = _read(row[0], "version_hash")
            status = "candidate"
            for event in events:
                if event["payload"].get("version_id") == version["id"] and event["kind"] in {
                    "accepted",
                    "rejected",
                    "retired",
                }:
                    status = event["kind"]
            versions.append({**version, "status": status})
        current = next(v for v in versions if v["id"] == head["current_version_id"])
        source = current["source"]
        uses = [
            _read(row[0], "use_hash")
            for row in db.execute(
                "SELECT payload FROM skill_card_uses WHERE project=? "
                "AND json_extract(payload,'$.card_id')=? ORDER BY rowid",
                (head["project"], head["id"]),
            )
        ]
        outcomes = [e for e in events if e["kind"] == "use_outcome"]
        failures = sum(e["payload"].get("technical_status") == "failed" for e in outcomes)
        consumed_runs = {e["payload"]["run_id"] for e in events if e["kind"] == "consumed"}
        return {
            **head,
            **current["definition"],
            "versions": versions,
            "source_summary": "从实际运行提炼，适用性仍需后续任务检验"
            if source.get("kind") == "run"
            else "由技能包导入，尚未在本系统运行验证",
            "use_count": len(consumed_runs),
            "bound_count": len(uses),
            "observed_outcomes": len(outcomes),
            "technical_failures": failures,
            "maintenance_notice": "多次执行遇到技术问题，建议检查适用范围" if failures >= 2 else "",
            "author_quality": "unknown",
        }

    def detail(self, project, card_id):
        with self.workflow.connection() as db:
            return self._detail(db, self._head(db, project, card_id))

    def list(self, project):
        with self.workflow.connection() as db:
            return [
                self._detail(db, _read(row[0], "card_hash"))
                for row in db.execute(
                    "SELECT payload FROM skill_cards WHERE project=? ORDER BY rowid DESC",
                    (project,),
                )
            ]

    def version(self, project, version_id):
        with self.workflow.connection() as db:
            return self._version(db, project, version_id)

    def decide(
        self,
        project,
        card_id,
        expected_card_hash,
        version_id,
        *,
        make_default=False,
        action="accepted",
    ):
        if action not in {"accepted", "rejected", "retired"}:
            raise ValueError("未声明的Skill决定")
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._head(db, project, card_id)
            if head["card_hash"] != expected_card_hash:
                raise ValueError("Skill已变化，请刷新后决定")
            version = self._version(db, project, version_id)
            if version["card_id"] != card_id:
                raise ValueError("版本不属于所选Skill")
            self._event(db, head, action, {"version_id": version_id, "make_default": make_default})
            active = (
                version_id if make_default and action == "accepted" else head["active_version_id"]
            )
            if action != "accepted" and active == version_id:
                active = None
            head = self._write_head(db, head, active_version_id=active)
            return self._detail(db, head)

    def enable(self, project, card_id, expected_card_hash, enabled):
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._head(db, project, card_id)
            if head["card_hash"] != expected_card_hash:
                raise ValueError("Skill已变化，请刷新后操作")
            head = self._write_head(db, head, enabled=enabled)
            self._event(db, head, "enabled" if enabled else "disabled", {})
            return self._detail(db, head)

    def rollback(self, project, card_id, expected_card_hash, version_id):
        card = self.detail(project, card_id)
        if not any(v["id"] == version_id and v["status"] == "accepted" for v in card["versions"]):
            raise ValueError("只能回退到明确采用过的版本")
        return self.decide(project, card_id, expected_card_hash, version_id, make_default=True)

    def resolve(self, project, selections=None, *, include_defaults=True):
        selected = [SkillSelection.model_validate(value) for value in selections or []]
        if len(selected) > 8 or len({value.card_id for value in selected}) != len(selected):
            raise ValueError("本次最多选用8项不同Skill")
        explicit = {value.card_id: value for value in selected}
        result = []
        for card in self.list(project):
            choice = explicit.get(card["id"])
            version_id = (
                choice.version_id
                if choice
                else card["active_version_id"]
                if include_defaults
                else None
            )
            if version_id is None:
                continue
            if not card["enabled"]:
                if choice:
                    raise ValueError("Skill已停用，不能继续选用")
                continue
            version = next((v for v in card["versions"] if v["id"] == version_id), None)
            if version is None or version["status"] in {"rejected", "retired"}:
                raise ValueError("Skill版本不存在或已撤回")
            if choice and choice.version_hash != version["version_hash"]:
                raise ValueError("Skill版本校验已变化")
            result.append(
                {
                    "card_id": card["id"],
                    "version_id": version["id"],
                    "version_hash": version["version_hash"],
                    "selection": "explicit_task" if choice else "adopted_default",
                    "definition": version["definition"],
                }
            )
        if set(explicit) - {r["card_id"] for r in result}:
            raise ValueError("所选Skill不属于当前工作区或已失效")
        if len(result) > 8:
            raise ValueError("已选和默认Skill合计超过8项，请缩小范围")
        return result

    def bind_graph(self, project, graph, selections=None, *, include_defaults=True):
        bindings = self.resolve(project, selections, include_defaults=include_defaults)
        spec = GraphSpecV2.model_validate(graph)
        operations = {node.operation for node in spec.nodes}
        for binding in bindings:
            for step in SkillDefinition.model_validate(binding["definition"]).steps:
                if step.operation and step.operation not in operations:
                    raise ValueError("当前流程没有Skill需要的制作步骤，请重新规划")
        nodes = []
        for node in spec.nodes:
            # Reusing a saved host graph does not silently carry old skill authorization.
            if "skill_card_base_guidance" in node.parameters:
                parameters = dict(node.parameters)
                base_guidance = parameters.pop("skill_card_base_guidance")
                base_templates = parameters.pop("skill_card_base_templates", [])
                parameters.pop("skill_card_versions", None)
                parameters["skill_entry_templates"] = base_templates
                node = node.model_copy(update={"guidance": base_guidance, "parameters": parameters})
            if node.operation not in _PRODUCTION_ROLES:
                nodes.append(node)
                continue
            templates = list(node.parameters.get("skill_entry_templates", []))
            guidance = [node.guidance]
            applied = []
            for binding in bindings:
                source = SourceRef(
                    kind="method", id=binding["version_id"], content_hash=binding["version_hash"]
                )
                used = False
                for step in SkillDefinition.model_validate(binding["definition"]).steps:
                    if step.operation in {"", node.operation}:
                        entry = step.entry("card_" + binding["card_id"][:12], source)
                        templates.append(entry.model_dump(mode="json"))
                        guidance.append("选用的Skill步骤：" + entry.text)
                        used = True
                if used:
                    applied.append(binding)
            nodes.append(
                node.model_copy(
                    update={
                        "guidance": "\n".join(guidance),
                        "parameters": {
                            **node.parameters,
                            "skill_entry_templates": templates,
                            "skill_card_base_guidance": node.guidance,
                            "skill_card_base_templates": node.parameters.get(
                                "skill_entry_templates", []
                            ),
                            "skill_card_versions": [
                                {k: b[k] for k in ("card_id", "version_id", "version_hash")}
                                for b in applied
                            ],
                        },
                    }
                )
                if applied
                else node
            )
        return spec.model_copy(update={"nodes": nodes}), bindings

    @staticmethod
    def validate_task_inputs(bindings, task, machine_requirements):
        from .evolution_v2 import render_parameterized_guidance
        from .graph_v2 import NodeSpecV2

        machine = machine_requirements or {}
        if hasattr(machine, "model_dump"):
            machine = machine.model_dump(mode="json")
        context = {
            "entities": list(task.base_bundle.get("entities", {}).values()),
            "machine_requirements": machine,
        }
        for binding in bindings:
            definition = SkillDefinition.model_validate(binding["definition"])
            source = SourceRef(
                kind="method", id=binding["version_id"], content_hash=binding["version_hash"]
            )
            for step in definition.steps:
                entry = step.entry("card_" + binding["card_id"][:12], source)
                node = NodeSpecV2(
                    id="check_inputs",
                    operation="plan_events",
                    product_key="check",
                    problem_key="check",
                    guidance=entry.text,
                    parameters={"skill_entry_templates": [entry.model_dump(mode="json")]},
                )
                try:
                    render_parameterized_guidance(node, context)
                except ValueError as exc:
                    raise ValueError(
                        "Skill“"
                        + definition.name
                        + "”缺少本次所需的角色或创作约定；请补充或取消选用"
                    ) from exc

    @staticmethod
    def clear_graph_bindings(graph):
        spec = GraphSpecV2.model_validate(graph)
        nodes = []
        for node in spec.nodes:
            if "skill_card_base_guidance" not in node.parameters:
                nodes.append(node)
                continue
            parameters = dict(node.parameters)
            guidance = parameters.pop("skill_card_base_guidance")
            templates = parameters.pop("skill_card_base_templates", [])
            parameters.pop("skill_card_versions", None)
            parameters["skill_entry_templates"] = templates
            nodes.append(node.model_copy(update={"guidance": guidance, "parameters": parameters}))
        return spec.model_copy(update={"nodes": nodes})

    def assert_bindings(self, project, bindings):
        for binding in bindings:
            card = self.detail(project, binding["card_id"])
            version = next((v for v in card["versions"] if v["id"] == binding["version_id"]), None)
            if (
                not card["enabled"]
                or version is None
                or version["version_hash"] != binding["version_hash"]
                or version["status"] in {"rejected", "retired"}
                or binding["selection"] == "adopted_default"
                and card["active_version_id"] != binding["version_id"]
            ):
                raise ValueError("Skill选择、版本或采用范围已变化，停止旧任务继续执行")

    def record_consumed(self, project, run_id, bindings, node_id):
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for binding in bindings:
                head = self._head(db, project, binding["card_id"])
                previous = db.execute(
                    "SELECT 1 FROM skill_card_events WHERE card_id=? "
                    "AND json_extract(payload,'$.kind')='consumed' "
                    "AND json_extract(payload,'$.payload.run_id')=? "
                    "AND json_extract(payload,'$.payload.node_id')=?",
                    (head["id"], run_id, node_id),
                ).fetchone()
                if not previous:
                    self._event(
                        db,
                        head,
                        "consumed",
                        {
                            "run_id": run_id,
                            "node_id": node_id,
                            "version_id": binding["version_id"],
                            "evidence": "provider_request_and_response",
                            "scope": "instructions_supplied_not_semantic_compliance",
                        },
                    )

    def record_use(self, project, run_id, bindings):
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for binding in bindings:
                identity = content_hash([run_id, binding["card_id"], binding["version_id"]])
                record = _seal(
                    {
                        "id": identity,
                        "run_id": run_id,
                        "project": project,
                        **{
                            k: binding[k]
                            for k in ("card_id", "version_id", "version_hash", "selection")
                        },
                        "created_at": now(),
                    },
                    "use_hash",
                )
                db.execute(
                    "INSERT OR IGNORE INTO skill_card_uses VALUES(?,?,?,?)",
                    (identity, run_id, project, json.dumps(record, ensure_ascii=False)),
                )

    def record_outcome(self, project, run_id, bindings, status):
        with self.workflow.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for binding in bindings:
                head = self._head(db, project, binding["card_id"])
                found = db.execute(
                    "SELECT 1 FROM skill_card_events WHERE card_id=? "
                    "AND json_extract(payload,'$.kind')='use_outcome' "
                    "AND json_extract(payload,'$.payload.run_id')=?",
                    (head["id"], run_id),
                ).fetchone()
                if found:
                    continue
                self._event(
                    db,
                    head,
                    "use_outcome",
                    {
                        "version_id": binding["version_id"],
                        "run_id": run_id,
                        "technical_status": "completed"
                        if status == "completed"
                        else "failed"
                        if status == "failed"
                        else "unknown",
                        "semantic_status": "unknown",
                        "author_quality": "unknown",
                        "attribution": "observed_use_not_causal_benefit",
                    },
                )
