"""Project-scoped experience clusters with frozen reads and append-only state."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..content.hash import content_hash
from .evolution_v2 import render_parameterized_guidance
from .graph_v2 import NodeSpecV2
from .memory_history import (
    build_history_payload,
    counter_for_identity,
    encoded,
    get_memory_token_counter,
    normal_visible_feedback,
    project_visible_messages,
)
from .models import new_id, now
from .skill_cards import SkillCards, SkillDefinition, SkillSelection
from .v2_evidence import _read_trace

_PRODUCTION = {
    "plan_events",
    "draft_quest",
    "draft_logic",
    "logic_contract",
    "draft_dialogue",
    "dialogue_constrained",
}
_NATIVE_REQUEST_PROTOCOL = "native-task-without-prior-advisory-v1"


class ClusterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["trajectory", "strategy", "skill"]
    description: str = Field(min_length=1, max_length=1000)
    contract: dict


def _seal(value, key):
    return {**value, key: content_hash(value)}


def _checked(value, key):
    record = json.loads(value) if isinstance(value, str) else value
    if record.get(key) != content_hash({k: v for k, v in record.items() if k != key}):
        raise ValueError("经历记录完整性校验失败")
    return record


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class MemoryClusters:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS creative_memory_clusters(
                    id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS creative_memory_states(
                    cluster_id TEXT NOT NULL,version INTEGER NOT NULL,project TEXT NOT NULL,
                    payload TEXT NOT NULL,PRIMARY KEY(cluster_id,version));
                CREATE TABLE IF NOT EXISTS creative_memory_members(
                    id TEXT PRIMARY KEY,cluster_id TEXT NOT NULL,project TEXT NOT NULL,
                    run_id TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(cluster_id,run_id));
            """)

    @staticmethod
    def _cluster(db, project, cluster_id):
        row = db.execute(
            "SELECT payload FROM creative_memory_clusters WHERE id=? AND project=?",
            (cluster_id, project),
        ).fetchone()
        if row is None:
            raise KeyError("经历簇不属于当前工作区或不存在")
        return _checked(row[0], "cluster_hash")

    @staticmethod
    def _state(db, project, cluster_id, version=None):
        condition, values = ("", ()) if version is None else (" AND version=?", (version,))
        row = db.execute(
            "SELECT payload FROM creative_memory_states WHERE cluster_id=? AND project=?"
            + condition
            + " ORDER BY version DESC LIMIT 1",
            (cluster_id, project, *values),
        ).fetchone()
        if row is None:
            raise ValueError("经历簇缺少指定状态版本")
        return _checked(row[0], "state_hash")

    @staticmethod
    def _summary(cluster, state):
        return {
            "id": cluster["id"],
            "backend": cluster["backend"],
            "description": cluster["description"],
            "completed_count": len(state["members"]),
            "state_version": state["version"],
            "state_hash": state["state_hash"],
            "cluster_hash": cluster["cluster_hash"],
            "contract": cluster["contract"],
        }

    def create(self, project, backend, description, *, contract, cluster_id=None):
        definition = ClusterDefinition(backend=backend, description=description, contract=contract)
        if not definition.description.strip() or not contract:
            raise ValueError("经历簇需要明确范围和执行合同")
        identity = cluster_id or new_id()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT payload FROM creative_memory_clusters WHERE id=?", (identity,)
            ).fetchone()
            if previous:
                cluster = _checked(previous[0], "cluster_hash")
                if cluster["project"] != project or any(
                    cluster[key] != value
                    for key, value in definition.model_dump(mode="json").items()
                ):
                    raise ValueError("已有经历簇的范围、机制和合同不可改写")
            else:
                cluster = _seal(
                    {
                        "schema_version": 1,
                        "id": identity,
                        "project": project,
                        **definition.model_dump(mode="json"),
                        "created_at": now(),
                    },
                    "cluster_hash",
                )
                state = _seal({"cluster_id": identity, "version": 0, "members": []}, "state_hash")
                db.execute(
                    "INSERT INTO creative_memory_clusters VALUES(?,?,?)",
                    (identity, project, _encoded(cluster)),
                )
                db.execute(
                    "INSERT INTO creative_memory_states VALUES(?,?,?,?)",
                    (identity, 0, project, _encoded(state)),
                )
            return self._summary(cluster, self._state(db, project, identity))

    def catalog(self, project, *, contract=None):
        with self.store.connection() as db:
            db.execute("BEGIN")
            clusters = [
                _checked(row[0], "cluster_hash")
                for row in db.execute(
                    "SELECT payload FROM creative_memory_clusters WHERE project=? ORDER BY rowid",
                    (project,),
                )
            ]
            return [
                self._summary(cluster, self._state(db, project, cluster["id"]))
                for cluster in clusters
                if contract is None or content_hash(cluster["contract"]) == content_hash(contract)
            ]

    def assert_cluster_sources(self, project, cluster_id, *, root, task, expected_contract=None):
        """Admit metadata before routing without rendering or exposing source content."""
        with self.store.connection() as db:
            db.execute("BEGIN")
            cluster = self._cluster(db, project, cluster_id)
            self._validate_contract(cluster, task, expected_contract)
            state = self._state(db, project, cluster_id)
            members = self._members(db, project, state)
        for _, source_state, _ in members:
            self._check_external(root, project, source_state)
        return self._summary(cluster, state)

    @staticmethod
    def _source_legacy(db, project, run_id, *, allow_waiting=False):
        row = db.execute(
            "SELECT v.payload FROM creative_v2_runs v JOIN runs r ON r.id=v.run_id "
            "JOIN tasks t ON t.id=r.task_id WHERE v.run_id=? AND v.project=? "
            "AND json_extract(t.payload,'$.project')=?",
            (run_id, project, project),
        ).fetchone()
        if row is None:
            raise KeyError("来源运行不属于当前工作区或不存在")
        state = json.loads(row[0])
        trace = _read_trace(
            db,
            {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
            project,
            run_id,
            [],
        )
        run = trace["run"]
        task = json.loads(
            db.execute("SELECT payload FROM tasks WHERE id=?", (run["task_id"],)).fetchone()[0]
        )
        if (
            state.get("run_id"),
            state.get("project"),
            state.get("task_id"),
            state.get("revision"),
        ) != (run_id, project, run["task_id"], run["revision"]):
            raise ValueError("来源运行的项目、任务或版本索引不一致")
        if task["revision"] != run["revision"] or run["cancel_requested"]:
            raise ValueError("来源运行已撤回或任务版本改变")
        allowed = {"completed", "completed_with_skips", "failed"}
        if allow_waiting:
            allowed.add("waiting_user")
        if run["status"] not in allowed:
            raise ValueError("仅已结束的实际运行可以写入经历")
        if content_hash(state["graph"]) != state["graph_hash"]:
            raise ValueError("来源工作图校验失败")
        if any(
            content_hash(ref["body"]) != ref["body_hash"] for ref in state.get("references", [])
        ):
            raise ValueError("来源参考内容校验失败")
        outputs = (state.get("result") or {}).get("outcomes", {})
        observed = any(
            node["operation"] in _PRODUCTION
            and outputs.get(node["id"], {}).get("status") in {"succeeded", "reused"}
            and outputs[node["id"]].get("outputs")
            for node in state["graph"]["nodes"]
        )
        snapshot = {
            "run_id": run_id,
            "task_id": task["id"],
            "revision": run["revision"],
            "request": task["request"],
            "base_hash": task["base_hash"],
            "status": run["status"],
            "graph_hash": state["graph_hash"],
            "requirements": state.get("requirements"),
            "references": state.get("references", []),
            "result": state.get("result"),
            "requests": trace["requests"],
            "results": [event for event in trace["events"] if event["type"] == "model/result"],
            "skill_bindings": state.get("skill_card_bindings", []),
            "host_contract_identity": state.get("host_contract_identity"),
            "has_production_outputs": bool(observed),
        }
        return state, snapshot

    @staticmethod
    def _active(cluster):
        return cluster["contract"].get("adapter_protocol") == "memory-routing-v3"

    @staticmethod
    def _observation_classification(source):
        requests, results = source["requests"], source["results"]
        if not requests:
            outcomes = (source.get("result") or {}).get("outcomes", {})
            return (
                "reused_only"
                if any(value.get("status") == "reused" for value in outcomes.values())
                else "outcome_unknown"
            )
        by_request = {event["payload"].get("request_id"): event["payload"] for event in results}
        if any(request["id"] not in by_request for request in requests) or any(
            result.get("outcome_known") is False for result in by_request.values()
        ):
            return "outcome_unknown"
        responses = [request for request in requests if "response" in by_request[request["id"]]]
        if not responses:
            return "provider_failure"
        outcomes = (source.get("result") or {}).get("outcomes", {})
        success = any(
            outcomes.get(request["node_id"], {}).get("status") == "succeeded"
            for request in responses
        )
        failure = source.get("status") == "failed" or any(
            value.get("status") == "failed" for value in outcomes.values()
        )
        if failure and success:
            return "mixed"
        return "observed_failure" if failure else "observed_success"

    @staticmethod
    def _predecessor(db, project, child, parent, parent_source):
        if (child["task_id"], child["revision"]) != (parent["task_id"], parent["revision"]):
            raise ValueError("经历前驱不属于同一任务和版本")
        repair = child.get("repair_contract")
        if repair:
            _checked(repair, "contract_hash")
            if (
                repair.get("project") != project
                or repair.get("task_id") != child["task_id"]
                or repair.get("revision") != child["revision"]
                or repair.get("source_run_id") != parent["run_id"]
                or repair.get("source_graph_hash") != parent["graph_hash"]
                or repair.get("source_result_hash") != content_hash(parent_source.get("result"))
                or repair.get("target_graph_hash") != child["graph_hash"]
            ):
                raise ValueError("经历修复链与真实前驱hash不一致")
            return {
                "kind": "repair",
                "run_id": parent["run_id"],
                "receipt_hash": repair["contract_hash"],
            }
        if child.get("stage_change_receipt_hash"):
            row = db.execute(
                "SELECT payload FROM creative_v2_stage_changes WHERE project=? AND next_run_id=?",
                (project, child["run_id"]),
            ).fetchone()
            if row is None:
                raise ValueError("经历阶段交接缺少正式前驱记录")
            receipt = _checked(row[0], "receipt_hash")
            if (
                receipt["receipt_hash"] != child["stage_change_receipt_hash"]
                or receipt["parent_run_id"] != parent["run_id"]
                or receipt["source_graph_hash"] != parent["graph_hash"]
                or receipt["target_graph_hash"] != child["graph_hash"]
            ):
                raise ValueError("经历阶段交接前驱hash不一致")
            return {
                "kind": "stage_change",
                "run_id": parent["run_id"],
                "receipt_hash": receipt["receipt_hash"],
            }
        if child["graph_hash"] != parent["graph_hash"]:
            child_graph = {key: value for key, value in child["graph"].items() if key != "id"}
            parent_graph = {key: value for key, value in parent["graph"].items() if key != "id"}
            if child_graph != parent_graph:
                raise ValueError("经历前驱图已变化但缺少已实现的交接合同")
        child_outcomes = (child.get("result") or {}).get("outcomes", {})
        parent_outcomes = (parent_source.get("result") or {}).get("outcomes", {})
        for node_id, outcome in child_outcomes.items():
            if outcome.get("status") != "reused":
                continue
            prior = parent_outcomes.get(node_id, {})
            if prior.get("status") not in {"succeeded", "reused"} or any(
                outcome.get(key) != prior.get(key)
                for key in ("operation", "signature", "outputs", "input_refs")
            ):
                raise ValueError("经历复用产物不匹配真实前驱签名与内容")
        return {
            "kind": "same_graph_continuation",
            "run_id": parent["run_id"],
            "source_hash": content_hash(parent_source),
        }

    @classmethod
    def _source(cls, db, project, run_id, *, source_schema_version=1):
        if source_schema_version != 3:
            return cls._source_legacy(db, project, run_id)
        chain, states, seen = [], [], set()
        current = run_id
        while current:
            if current in seen or len(seen) >= 128:
                raise ValueError("经历前驱链循环或超过已实现边界")
            seen.add(current)
            state, native = cls._source_legacy(
                db, project, current, allow_waiting=current != run_id
            )
            trace = _read_trace(
                db,
                {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                project,
                current,
                [],
            )
            order = [
                {
                    "request_id": event["payload"]["id"],
                    "seq": event["seq"],
                    "event_hash": event["event_hash"],
                }
                for event in trace["events"]
                if event["type"] == "model/request"
            ]
            if [item["request_id"] for item in order] != [
                item["id"] for item in native["requests"]
            ]:
                raise ValueError("经历实际请求缺少唯一的顺序事件")
            chain.append(
                {
                    "run_id": current,
                    "task_id": state["task_id"],
                    "revision": state["revision"],
                    "source_hash": content_hash(native),
                    "source": native,
                    "request_order": order,
                    "feedback_events": normal_visible_feedback(trace["events"]),
                    "predecessor": None,
                }
            )
            states.append(state)
            current = state.get("prior_run_id")
        for index in range(len(chain) - 1):
            chain[index]["predecessor"] = cls._predecessor(
                db, project, states[index], states[index + 1], chain[index + 1]["source"]
            )
        root_state, root_source = states[0], chain[0]["source"]
        source = {
            **root_source,
            "source_schema_version": 3,
            "source_chain": list(reversed(chain)),
            "current_observation_classification": cls._observation_classification(root_source),
        }
        labels = {cls._observation_classification(item["source"]) for item in chain}
        source["observation_classification"] = (
            "mixed"
            if "mixed" in labels or {"observed_success", "observed_failure"} <= labels
            else source["current_observation_classification"]
        )
        source["visible_messages"] = project_visible_messages(source)
        return {**root_state, "_memory_predecessor_states": states[1:]}, source

    def _check_external(self, root, project, state):
        from .v2_service import _check_references, check_narrative_snapshot

        _check_references(root, state.get("references", []))
        check_narrative_snapshot(self.store, project, state)
        if state.get("skill_card_bindings"):
            SkillCards(self.store).assert_bindings(project, state["skill_card_bindings"])
        for predecessor in state.get("_memory_predecessor_states", []):
            self._check_external(root, project, predecessor)

    @staticmethod
    def _validate_contract(cluster, task, expected_contract):
        if task.project != cluster["project"]:
            raise ValueError("经历簇不能跨工作区读取")
        if expected_contract is not None and content_hash(cluster["contract"]) != content_hash(
            expected_contract
        ):
            raise ValueError("经历机制的模型、角色或schema合同已变化")
        if task.request.model and cluster["contract"].get("model") != task.request.model:
            raise ValueError("经历簇的模型合同与当前任务不一致")

    def _members(self, db, project, state):
        result = []
        for ref in state["members"]:
            row = db.execute(
                "SELECT payload FROM creative_memory_members "
                "WHERE id=? AND project=? AND cluster_id=?",
                (ref["id"], project, state["cluster_id"]),
            ).fetchone()
            if row is None:
                raise ValueError("经历簇缺少来源成员")
            member = _checked(row[0], "member_hash")
            if member["member_hash"] != ref["member_hash"]:
                raise ValueError("经历成员版本已变化")
            if content_hash(member["source"]) != member["source_hash"]:
                raise ValueError("经历成员原始来源hash不一致")
            source_state, source = self._source(
                db,
                project,
                member["run_id"],
                source_schema_version=member.get("source", {}).get("source_schema_version", 1),
            )
            if content_hash(source) != member["source_hash"]:
                raise ValueError("经历来源已改变，旧读取不可继续")
            result.append((member, source_state, source))
        return result

    @staticmethod
    def _bounded(units, max_chars):
        selected = []
        used = 0
        for unit in reversed(units):
            size = len(_encoded(unit))
            if used + size <= max_chars:
                selected.append(unit)
                used += size
        return list(reversed(selected)), len(units) - len(selected)

    @staticmethod
    def _native_request(record):
        """Project the current task while retaining a pointer to the unmodified request."""
        if content_hash(record["request"]) != record["request_hash"]:
            raise ValueError("经历原始请求校验失败")
        projected = json.loads(_encoded(record))
        original_user = record["request"].get("user")
        removed = None
        applied = False
        if isinstance(original_user, str):
            try:
                context = json.loads(original_user)
            except ValueError:
                context = None
            if isinstance(context, dict) and "past_experience" in context:
                removed = content_hash(context.pop("past_experience"))
                projected["request"]["user"] = _encoded(context)
                applied = True
        manifest = record.get("context_manifest") or {}
        projected["reconstruction_scope"] = "native_task_projection_with_original_harness_pointer"
        projected["projection"] = {
            "protocol": _NATIVE_REQUEST_PROTOCOL,
            "applied": applied,
            "removed_fields": ["request.user.past_experience"] if applied else [],
            "original_request_hash": record["request_hash"],
            "projected_request_hash": content_hash(projected["request"]),
            "removed_past_experience_hash": removed,
            "memory_route_hash": manifest.get("memory_route_hash"),
            "memory_snapshot_hash": manifest.get("memory_snapshot_hash"),
            "original_reconstruction_scope": record.get("reconstruction_scope"),
            "source_ref": {
                "run_id": record["run_id"],
                "request_id": record["id"],
                "request_hash": record["request_hash"],
                "context_hash": record["context_hash"],
            },
        }
        return projected

    def read_snapshot(
        self,
        project,
        cluster_id,
        *,
        root,
        task,
        machine_requirements=None,
        explicit_skill_selections=(),
        include_project_skills=True,
        max_chars=32000,
        max_tokens=12000,
        counter=None,
        expected_contract=None,
    ):
        if type(max_chars) is not int or not 1024 <= max_chars <= 128000:
            raise ValueError("经历上下文预算应为1024至128000字符")
        with self.store.connection() as db:
            db.execute("BEGIN")
            cluster = self._cluster(db, project, cluster_id)
            self._validate_contract(cluster, task, expected_contract)
            state = self._state(db, project, cluster_id)
            members = self._members(db, project, state)
            card_details = self._skill_catalog(db, project) if cluster["backend"] == "skill" else {}
        for _, source_state, _ in members:
            self._check_external(root, project, source_state)
        refs = [
            {
                "kind": "run",
                "id": member["run_id"],
                "source_hash": member["source_hash"],
                "member_hash": member["member_hash"],
            }
            for member, _, _ in members
        ]
        if self._active(cluster):
            if cluster["backend"] == "skill":
                raise ValueError("旧Skill读取后端不属于新版学习机制库")
            counter = counter or get_memory_token_counter()
            parameters = {
                "machine_requirements": machine_requirements.model_dump(mode="json")
                if hasattr(machine_requirements, "model_dump")
                else machine_requirements or {},
            }
            payload, lineage, budget, retrieval = self._active_payload(
                cluster, members, task, parameters, max_tokens, counter
            )
            return _seal(
                {
                    "schema_version": 3,
                    "project": project,
                    "cluster_id": cluster_id,
                    "backend": cluster["backend"],
                    "cluster_hash": cluster["cluster_hash"],
                    "state_version": state["version"],
                    "state_hash": state["state_hash"],
                    "contract": cluster["contract"],
                    "payload": payload,
                    "source_refs": refs,
                    "lineage": lineage,
                    "token_budget": budget,
                    "read_parameters": parameters,
                    "retrieval": retrieval,
                    "omitted_units": budget["omitted_messages"],
                    "already_bound_skill_versions": [],
                    "task_id": task.id,
                    "task_revision": task.revision,
                    "task_input_hash": content_hash(task.request),
                },
                "snapshot_hash",
            )
        if cluster["backend"] == "trajectory":
            units = []
            for member, _, source in members:
                for request in source["requests"]:
                    results = [
                        event["payload"]
                        for event in source["results"]
                        if event["payload"].get("request_id") == request["id"]
                    ]
                    units.append(
                        {
                            "source_run_id": member["run_id"],
                            "source_request": self._native_request(request),
                            "observed_results": results,
                            "outcome_scope": "observed_history_not_current_facts_or_quality",
                        }
                    )
            selected, omitted = self._bounded(units, max_chars)
            payload = {
                "backend": "trajectory",
                "view_protocol": _NATIVE_REQUEST_PROTOCOL,
                "units": selected,
            }
            already_bound = []
        else:
            payload, omitted, already_bound = self._skill_payload(
                project,
                task,
                members,
                machine_requirements,
                explicit_skill_selections,
                include_project_skills,
                max_chars,
                card_details,
            )
        snapshot = {
            "schema_version": 1,
            "project": project,
            "cluster_id": cluster_id,
            "backend": cluster["backend"],
            "cluster_hash": cluster["cluster_hash"],
            "state_version": state["version"],
            "state_hash": state["state_hash"],
            "contract": cluster["contract"],
            "payload": payload,
            "source_refs": refs,
            "omitted_units": omitted,
            "max_chars": max_chars,
            "budget_unit": "unicode_characters_not_tokens",
            "already_bound_skill_versions": already_bound,
            "task_id": task.id,
            "task_revision": task.revision,
            "task_input_hash": content_hash(task.request),
        }
        return _seal(snapshot, "snapshot_hash")

    @staticmethod
    def _active_payload(
        cluster, members, task, parameters, max_tokens, counter, *, frozen_retrieval=None
    ):
        if cluster["backend"] == "trajectory":
            payload, lineage, budget = build_history_payload(members, max_tokens, counter)
            return payload, lineage, budget, None
        if cluster["backend"] != "strategy":
            raise ValueError("新版经历后端未实现")
        from .memory_lessons import rebuild_experience_lessons, select_experience_lessons

        if frozen_retrieval is not None:
            retrieval = frozen_retrieval
            payload = rebuild_experience_lessons(
                members, task, parameters["machine_requirements"], retrieval, counter, max_tokens
            )
        else:
            payload, retrieval = select_experience_lessons(
                members, task, parameters["machine_requirements"], counter, max_tokens
            )
        tokens = counter.count(encoded(payload))
        if tokens > max_tokens:
            raise ValueError("检索策略超过冻结token预算")
        budget = {
            "max_tokens": max_tokens,
            "token_count": tokens,
            "counter_identity": counter.identity,
            "omitted_messages": 0,
            "selection_rule": "most_similar_prior_request",
            "accounting_scope": "serialized_advisory_not_total_provider_context",
        }
        lineage = {
            "projection_protocol": "experience-lessons-v1",
            "source_chain": [
                {
                    key: episode[key]
                    for key in ("run_id", "task_id", "revision", "source_hash", "predecessor")
                }
                for _, _, source in members
                for episode in source["source_chain"]
            ],
        }
        return payload, lineage, budget, retrieval

    @staticmethod
    def _skill_catalog(db, project):
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_cards'"
            ).fetchone()
            is None
        ):
            return {}
        result = {}
        for row in db.execute("SELECT payload FROM skill_cards WHERE project=?", (project,)):
            card = _checked(row[0], "card_hash")
            events = [
                _checked(event[0], "event_hash")
                for event in db.execute(
                    "SELECT payload FROM skill_card_events "
                    "WHERE project=? AND card_id=? ORDER BY seq",
                    (project, card["id"]),
                )
            ]
            versions = []
            for raw in db.execute(
                "SELECT payload FROM skill_card_versions "
                "WHERE project=? AND card_id=? ORDER BY rowid",
                (project, card["id"]),
            ):
                version = _checked(raw[0], "version_hash")
                status = "candidate"
                for event in events:
                    if event["payload"].get("version_id") == version["id"] and event["kind"] in {
                        "accepted",
                        "rejected",
                        "retired",
                    }:
                        status = event["kind"]
                versions.append({**version, "status": status})
            result[card["id"]] = {**card, "versions": versions}
        return result

    def _skill_payload(
        self, project, task, members, machine, explicit, include_defaults, budget, card_details
    ):
        selections = [SkillSelection.model_validate(value) for value in explicit]
        if len(selections) > 8 or len({value.card_id for value in selections}) != len(selections):
            raise ValueError("本次Skill选择重复或超过8项")
        base_by_card = {}
        explicit_ids = {value.version_id for value in selections}
        for value in selections:
            card = card_details.get(value.card_id)
            version = next(
                (
                    item
                    for item in (card or {}).get("versions", [])
                    if item["id"] == value.version_id
                ),
                None,
            )
            if (
                not card
                or not card["enabled"]
                or not version
                or version["version_hash"] != value.version_hash
                or version["status"] in {"rejected", "retired"}
            ):
                raise ValueError("本次明确选用的Skill已失效")
            base_by_card[value.card_id] = value.version_id
        if include_defaults:
            for card in card_details.values():
                if card["enabled"] and card["active_version_id"] and card["id"] not in base_by_card:
                    base_by_card[card["id"]] = card["active_version_id"]
        base_ids = set(base_by_card.values())
        units, seen, already_bound, excluded = [], set(), [], 0
        context = {
            "entities": list(task.base_bundle.get("entities", {}).values()),
            "machine_requirements": machine.model_dump(mode="json")
            if hasattr(machine, "model_dump")
            else machine or {},
        }
        for member, _, source in members:
            for binding in source["skill_bindings"]:
                if binding["version_id"] in seen:
                    continue
                seen.add(binding["version_id"])
                card = card_details.get(binding["card_id"])
                version = next(
                    (
                        v
                        for v in (card or {}).get("versions", [])
                        if v["id"] == binding["version_id"]
                    ),
                    None,
                )
                if (
                    not card
                    or not card["enabled"]
                    or not version
                    or version["version_hash"] != binding["version_hash"]
                    or (version["status"] != "accepted" and version["id"] not in explicit_ids)
                ):
                    excluded += 1
                    continue
                if version["id"] in base_ids:
                    already_bound.append(version["id"])
                    continue
                if card["id"] in base_by_card:
                    excluded += 1
                    continue
                definition = SkillDefinition.model_validate(version["definition"])
                steps = []
                from .evolution import SourceRef

                for step in definition.steps:
                    entry = step.entry(
                        "card_" + card["id"][:12],
                        SourceRef(
                            kind="method", id=version["id"], content_hash=version["version_hash"]
                        ),
                    )
                    node = NodeSpecV2(
                        id="memory_read",
                        operation="plan_events",
                        product_key="memory",
                        problem_key="read",
                        guidance=entry.text,
                        parameters={"skill_entry_templates": [entry.model_dump(mode="json")]},
                    )
                    steps.append(
                        {
                            "id": step.id,
                            "title": step.title,
                            "operation": step.operation,
                            "instruction": render_parameterized_guidance(node, context),
                        }
                    )
                units.append(
                    {
                        "card_id": card["id"],
                        "source_run_id": member["run_id"],
                        "member_hash": member["member_hash"],
                        "version_id": version["id"],
                        "version_hash": version["version_hash"],
                        "name": definition.name,
                        "purpose": definition.purpose,
                        "steps": steps,
                        "applies_to": definition.applies_to,
                        "does_not_apply_to": definition.does_not_apply_to,
                    }
                )
        selected, omitted = self._bounded(units, budget)
        return {"backend": "skill", "skills": selected}, omitted + excluded, already_bound

    def assert_snapshot(
        self, project, snapshot, *, root, task, expected_contract=None, counter=None
    ):
        snapshot = _checked(snapshot, "snapshot_hash")
        if (
            snapshot["project"],
            snapshot["task_id"],
            snapshot["task_revision"],
            snapshot["task_input_hash"],
        ) != (project, task.id, task.revision, content_hash(task.request)):
            raise ValueError("经历读取不属于当前任务版本")
        with self.store.connection() as db:
            db.execute("BEGIN")
            cluster = self._cluster(db, project, snapshot["cluster_id"])
            self._validate_contract(cluster, task, expected_contract)
            if (
                cluster["cluster_hash"] != snapshot["cluster_hash"]
                or cluster["contract"] != snapshot["contract"]
            ):
                raise ValueError("经历簇定义已改变")
            state = self._state(db, project, cluster["id"], snapshot["state_version"])
            if state["state_hash"] != snapshot["state_hash"]:
                raise ValueError("冻结经历状态版本不一致")
            members = self._members(db, project, state)
            card_details = (
                self._skill_catalog(db, project) if snapshot["backend"] == "skill" else {}
            )
        for _, source_state, _ in members:
            self._check_external(root, project, source_state)
        current_refs = [
            {
                "kind": "run",
                "id": member["run_id"],
                "source_hash": member["source_hash"],
                "member_hash": member["member_hash"],
            }
            for member, _, _ in members
        ]
        if snapshot["source_refs"] != current_refs:
            raise ValueError("冻结经历来源关联不一致")
        if self._active(cluster):
            if snapshot.get("schema_version") != 3 or snapshot["backend"] != cluster["backend"]:
                raise ValueError("冻结经历后端或投影协议不一致")
            budget = snapshot["token_budget"]
            counter = counter_for_identity(budget["counter_identity"], counter=counter)
            payload, lineage, rebuilt_budget, retrieval = self._active_payload(
                cluster,
                members,
                task,
                snapshot["read_parameters"],
                budget["max_tokens"],
                counter,
                frozen_retrieval=snapshot.get("retrieval"),
            )
            if any(
                snapshot[key] != expected
                for key, expected in (
                    ("payload", payload),
                    ("lineage", lineage),
                    ("token_budget", rebuilt_budget),
                    ("retrieval", retrieval),
                    ("omitted_units", rebuilt_budget["omitted_messages"]),
                )
            ):
                raise ValueError("冻结经历正文、反馈、顺序或预算与真实来源不一致")
            return snapshot
        for unit in snapshot["payload"].get("skills", []):
            card = card_details.get(unit["card_id"])
            if card is None:
                raise ValueError("经历使用的Skill来源缺失")
            version = next((v for v in card["versions"] if v["id"] == unit["version_id"]), None)
            if (
                not card["enabled"]
                or not version
                or version["version_hash"] != unit["version_hash"]
                or version["status"] in {"rejected", "retired"}
            ):
                raise ValueError("经历使用的Skill已撤回或改变")
        return snapshot

    def append_run(
        self,
        project,
        cluster_id,
        run_id,
        *,
        expected_state_hash,
        root,
        lesson_provider=None,
        encoder=None,
        counter=None,
    ):
        with self.store.connection() as db:
            db.execute("BEGIN")
            cluster = self._cluster(db, project, cluster_id)
            source_state, source = self._source(
                db, project, run_id, source_schema_version=3 if self._active(cluster) else 1
            )
        route = _checked(source_state.get("memory_route") or {}, "receipt_hash")
        if self._active(cluster):
            from .memory_routing import assert_memory_route

            assert_memory_route(self.store, root, self.store.get_task(source["task_id"]), route)
        snapshot = route.get("read_snapshot") or {}
        if (
            route.get("cluster_id") != cluster_id
            or snapshot.get("cluster_id") != cluster_id
            or route.get("cluster_hash") != cluster["cluster_hash"]
        ):
            raise ValueError("只能更新本次正式路由选定的经历簇")
        _checked(snapshot, "snapshot_hash")
        if (
            snapshot["state_hash"] != expected_state_hash
            or snapshot["contract"] != cluster["contract"]
            or any(
                route.get(key) != snapshot[key]
                for key in ("backend", "cluster_id", "cluster_hash", "state_hash", "contract")
            )
        ):
            raise ValueError("经历写入与本次冻结路由不一致")
        self.assert_snapshot(
            project,
            snapshot,
            root=root,
            task=self.store.get_task(source["task_id"]),
            expected_contract=cluster["contract"],
            counter=counter,
        )
        self._check_external(root, project, source_state)
        eligible = source.get("current_observation_classification") in {
            "observed_success",
            "observed_failure",
            "mixed",
        }
        learning = {
            "observation_kind": source.get("current_observation_classification"),
            "learning_status": "not_requested",
            "lesson_count": 0,
            "formation_run_id": None,
        }
        if (self._active(cluster) and not eligible) or (
            not self._active(cluster) and not source["has_production_outputs"]
        ):
            with self.store.connection() as db:
                current = self._state(db, project, cluster_id)
            return {
                **self._summary(cluster, current),
                "status": "no_change",
                "reason": "本次没有可确认的新回应，不增加经历成员"
                if self._active(cluster)
                else "没有实际制作结果，未学习服务故障",
                "observation_classification": source.get("current_observation_classification"),
                **{**learning, "learning_status": "no_change"},
            }
        formation = None
        with self.store.connection() as db:
            db.execute("BEGIN")
            latest = self._state(db, project, cluster_id)
            previous = db.execute(
                "SELECT payload FROM creative_memory_members WHERE cluster_id=? AND run_id=?",
                (cluster_id, run_id),
            ).fetchone()
            if previous:
                member = _checked(previous[0], "member_hash")
                if member["source_hash"] != content_hash(source):
                    raise ValueError("已有来源运行不可重写")
                prior_formation = member.get("lesson_formation")
                if prior_formation:
                    _checked(prior_formation, "formation_hash")
                    learning.update(
                        learning_status="completed"
                        if prior_formation.get("items")
                        else "no_change",
                        lesson_count=len(prior_formation.get("items", [])),
                        formation_run_id=prior_formation.get("formation_run_id"),
                    )
                return {**self._summary(cluster, latest), "status": "already_recorded", **learning}
            if latest["state_hash"] != expected_state_hash:
                raise ValueError("经历簇已新增结果，本次写入待重新对账，不重跑制作")
        if self._active(cluster) and cluster["backend"] == "strategy":
            from .memory_lessons import form_experience_lessons

            formation = form_experience_lessons(
                self.store,
                root,
                project,
                cluster_id,
                run_id,
                source,
                provider=lesson_provider,
                encoder=encoder,
            )
            _checked(formation, "formation_hash")
            learning.update(
                learning_status="completed" if formation.get("items") else "no_change",
                lesson_count=len(formation.get("items", [])),
                formation_run_id=formation.get("formation_run_id"),
            )
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self._state(db, project, cluster_id)
            previous = db.execute(
                "SELECT payload FROM creative_memory_members WHERE cluster_id=? AND run_id=?",
                (cluster_id, run_id),
            ).fetchone()
            if previous:
                member = _checked(previous[0], "member_hash")
                if member["source_hash"] != content_hash(source):
                    raise ValueError("已有来源运行不可重写")
                return {**self._summary(cluster, latest), "status": "already_recorded", **learning}
            if latest["state_hash"] != expected_state_hash:
                raise ValueError("经历簇已新增结果，本次写入待重新对账，不重跑制作")
            _, current = self._source(
                db, project, run_id, source_schema_version=source.get("source_schema_version", 1)
            )
            if content_hash(current) != content_hash(source):
                raise ValueError("经历来源在写入前改变")
            member = _seal(
                {
                    "schema_version": 1,
                    "id": content_hash([cluster_id, run_id]),
                    "cluster_id": cluster_id,
                    "project": project,
                    "run_id": run_id,
                    "source_hash": content_hash(source),
                    "source": source,
                    **({"lesson_formation": formation} if formation is not None else {}),
                    "created_at": now(),
                },
                "member_hash",
            )
            db.execute(
                "INSERT INTO creative_memory_members VALUES(?,?,?,?,?)",
                (member["id"], cluster_id, project, run_id, _encoded(member)),
            )
            state = _seal(
                {
                    "cluster_id": cluster_id,
                    "version": latest["version"] + 1,
                    "members": latest["members"]
                    + [{"id": member["id"], "member_hash": member["member_hash"]}],
                },
                "state_hash",
            )
            db.execute(
                "INSERT INTO creative_memory_states VALUES(?,?,?,?)",
                (cluster_id, state["version"], project, _encoded(state)),
            )
            return {**self._summary(cluster, state), "status": "appended", **learning}


def read_memory_source(db, project, run_id, *, source_schema_version=3):
    """SELECT-only source reconstruction; does not initialize a store or schema."""
    return MemoryClusters._source(db, project, run_id, source_schema_version=source_schema_version)
