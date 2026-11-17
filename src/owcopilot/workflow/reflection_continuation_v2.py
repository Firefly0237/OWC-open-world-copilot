"""Exact slot normalization and read-only compatibility with historic continuations.

Original model text, failed episode and budget history stay immutable. The
normalizer changes representations only; it cannot repair meaning or invent
bindings. A continuation has its own append-only claim/result, never another
Reflector, family, episode or allowance. New attempts use the policy ledger.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..content.hash import content_hash
from .evolution import SourceRef
from .evolution_v2 import (
    ReflectionReport,
    verify_grounding,
)
from .harness import BudgetOwner
from .models import now

POLICY_VERSION = "exact-frozen-slot-bindings-v1"
_NAMESPACE = "machine_requirements."
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _same_type_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_type_value(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_type_value(v, right[k]) for k, v in left.items()
        )
    return bool(left == right)


def available_parameter_bindings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = snapshot["machine_requirements"]
    entities = snapshot["entities"]
    bindings = []
    for key, value in requirements.items():
        if key == "schema_version" or value is None:
            continue
        bindings.append(
            {
                "kind": "requirement",
                "binding_key": key,
                "source_value": str(value),
                "typed_value_json": _json(value),
                "value_type": type(value).__name__,
            }
        )
    for index, entity_id in enumerate(requirements.get("participant_ids", [])):
        if entity_id not in entities:
            raise ValueError("冻结参与者未绑定到真实项目实体")
        bindings.append(
            {
                "kind": "entity",
                "binding_key": f"participant:{index}",
                "source_value": entity_id,
                "typed_value_json": _json(entity_id),
                "value_type": "entity_id",
            }
        )
    return bindings


def bound_parameter_schema(model, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Constrain kind/key/value together; independent enums permit invalid cross-pairs."""
    schema = model.model_json_schema()
    variants = []
    for binding in available_parameter_bindings(snapshot):
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "kind", "binding_key", "source_value"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"},
                    **{
                        key: {"type": "string", "const": binding[key]}
                        for key in ("kind", "binding_key", "source_value")
                    },
                },
            }
        )
    slot_schema: dict[str, Any] = {"oneOf": variants} if variants else {"not": {}}
    definitions = schema.setdefault("$defs", {})
    if "ParameterSlot" in definitions:
        definitions["ParameterSlot"] = slot_schema
    if "TemplateBinding" in definitions:
        variants = [
            {
                "properties": {
                    "binding_kind": {"const": row["kind"]},
                    "binding_key": {"const": row["binding_key"]},
                }
            }
            for row in available_parameter_bindings(snapshot)
        ]
        admission: dict[str, Any] = {"oneOf": variants} if variants else {"not": {}}
        definitions["TemplateBinding"]["allOf"] = [admission]
    return schema


def _parse_report(raw_text: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("原始反思JSON含重复键，不能决定其真实含义")
            result[key] = value
        return result

    blocks = re.findall(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)```", raw_text.strip())
    if len(blocks) > 1:
        raise ValueError("反思含多个JSON候选，不能任意选择")
    value = json.loads(blocks[0] if blocks else raw_text, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("反思必须为JSON对象")
    return value


def normalize_reflection_report(
    raw_text: str,
    snapshot: dict[str, Any],
    allowed_refs: list[SourceRef],
    *,
    author_feedback: bool = False,
) -> dict[str, Any]:
    original = _parse_report(raw_text)
    normalized = json.loads(_json(original))
    requirements = snapshot["machine_requirements"]
    entities = snapshot["entities"]
    name_ids: dict[str, list[str]] = {}
    for entity_id, entity in entities.items():
        if isinstance(entity.get("name"), str):
            name_ids.setdefault(entity["name"], []).append(entity_id)
    transforms = []
    for entry in normalized.get("reusable_entries", []):
        for slot in entry.get("slots", []):
            before = dict(slot)
            rules = []
            if slot["kind"] == "requirement":
                key = slot["binding_key"]
                if key.startswith(_NAMESPACE):
                    key = key[len(_NAMESPACE) :]
                    rules.append("strip_declared_machine_requirements_namespace")
                if key not in requirements or key == "schema_version" or requirements[key] is None:
                    raise ValueError("未知或未声明的机器要求绑定，禁止猜测alias")
                expected = requirements[key]
                raw = slot["source_value"]
                if raw == str(expected):
                    rules.append("exact_existing_internal_representation")
                else:
                    try:
                        decoded = json.loads(raw)
                    except (ValueError, TypeError):
                        decoded = None
                    if _same_type_value(decoded, expected):
                        rules.append("strict_typed_json_same_frozen_value")
                    elif (
                        isinstance(expected, list)
                        and expected
                        and all(isinstance(v, str) and _IDENTIFIER.fullmatch(v) for v in expected)
                        and raw == ",".join(expected)
                    ):
                        rules.append("exact_comma_render_of_delimiter_free_frozen_identifiers")
                    else:
                        raise ValueError("source_value与冻结typed实际值不相同或分隔有歧义")
                slot.update(binding_key=key, source_value=str(expected))
                typed_json = _json(expected)
            elif slot["kind"] == "entity":
                match = re.fullmatch(r"participant:(0|[1-9][0-9]*)", slot["binding_key"])
                participants = requirements.get("participant_ids", [])
                if match is None or int(match[1]) >= len(participants):
                    raise ValueError("实体槽必须绑定冻结的明确参与者索引")
                raw = slot["source_value"]
                candidates = set(name_ids.get(raw, [])) | ({raw} if raw in entities else set())
                if len(candidates) != 1:
                    raise ValueError("源实体name不唯一或未知，禁止模糊alias匹配")
                entity_id = next(iter(candidates))
                if participants[int(match[1])] != entity_id:
                    raise ValueError("实体值与冻结参与者索引不一致")
                slot["source_value"] = entity_id
                rules.append(
                    "exact_frozen_unique_name_to_id"
                    if raw != entity_id
                    else "exact_frozen_entity_id"
                )
                typed_json = _json(entity_id)
            else:
                raise ValueError("未知参数种类")
            transforms.append(
                {
                    "entry_id": entry["id"],
                    "slot_name": slot["name"],
                    "before": before,
                    "after": dict(slot),
                    "rules": rules,
                    "typed_value_json": typed_json,
                }
            )
    report = ReflectionReport.model_validate(normalized)
    # Preserve the original strict verifier; representation normalization grants no new truth.
    verify_grounding(
        report, None, allowed_refs, set(entities), requirements, author_feedback=author_feedback
    )
    payload = {
        "policy_version": POLICY_VERSION,
        "raw_text": raw_text,
        "raw_text_hash": content_hash(raw_text),
        "raw_report_hash": content_hash(original),
        "snapshot_hash": content_hash(snapshot),
        "normalized_report": report.model_dump(mode="json"),
        "transforms": transforms,
        "semantic_changes": "none",
        "grounding": "verified",
    }
    return {**payload, "normalization_hash": content_hash(payload)}


def _unseal(raw: str, field: str) -> dict[str, Any]:
    record = json.loads(raw)
    if record.get(field) != content_hash({k: v for k, v in record.items() if k != field}):
        raise ValueError("续接来源持久hash不一致：" + field)
    return record


def inspect_reflection_continuation(
    store, root, project, episode_id, trace_run_id
) -> dict[str, Any]:
    """Read-only eligibility, immutable source evidence, exact owner and normalization proof."""
    from .v2_service import _check_references, v2_detail

    with store.connection() as db:
        episode_row = db.execute(
            "SELECT payload FROM evolution_episodes WHERE id=? AND project=?",
            (episode_id, project),
        ).fetchone()
        result_row = db.execute(
            "SELECT payload FROM evolution_episode_results WHERE episode_id=? AND project=?",
            (episode_id, project),
        ).fetchone()
        if episode_row is None or result_row is None:
            raise ValueError("缺少原episode或不可变终态")
        episode = _unseal(episode_row[0], "episode_hash")
        result = _unseal(result_row[0], "result_hash")
        if (
            result["status"] != "failed"
            or result["candidate_version_id"] is not None
            or "参数化经验" not in result["evidence"].get("error", "")
        ):
            raise ValueError("续接只适用于尚未形成候选的槽协议grounding失败")
        rows = db.execute(
            "SELECT request FROM creative_harness_dispatches WHERE run_id=? ORDER BY attempt",
            (trace_run_id,),
        ).fetchall()
        requests = [json.loads(row[0]) for row in rows]
        if len(requests) != 2 or [r["attempt"] for r in requests] != [1, 2]:
            raise ValueError("原轨迹必须只有两次Reflector派发")
        for request in requests:
            if (
                request["request_hash"] != content_hash(request["request"])
                or request["context_hash"] != content_hash(request["context_manifest"])
                or request["request"]["role_id"] != "method_reflector"
            ):
                raise ValueError("原始请求hash或角色不合法")
        if (
            requests[0]["owner"] != requests[1]["owner"]
            or requests[0]["budget_id"] != requests[1]["budget_id"]
        ):
            raise ValueError("两次原调用不属于同一预算owner")
        owner = BudgetOwner.model_validate(requests[0]["owner"])
        if (
            owner.project != project
            or owner.product_key != "method-evolution/" + episode["family_id"]
            or owner.problem_key != episode["budget_owner"]
        ):
            raise ValueError("原owner与episode身份不一致")
        bound = db.execute(
            "SELECT budget_id,payload FROM creative_harness_owners WHERE owner_key=?",
            (owner.identity,),
        ).fetchone()
        if bound is None or bound[0] != requests[0]["budget_id"]:
            raise ValueError("原owner预算绑定已改变")
        attempts = db.execute(
            "SELECT attempts FROM node_budgets WHERE id=?", (bound[0],)
        ).fetchone()[0]
        context = json.loads(requests[0]["request"]["messages"][-1]["content"])
        snapshot = context["source"]
        experience = _unseal(
            db.execute(
                "SELECT payload FROM evolution_experiences WHERE id=? AND project=?",
                (episode["experience_id"], project),
            ).fetchone()[0],
            "experience_hash",
        )
        parent = _unseal(
            db.execute(
                "SELECT payload FROM evolution_versions WHERE id=? AND project=?",
                (episode["parent_version_id"], project),
            ).fetchone()[0],
            "version_hash",
        )
        if (
            experience["experience_hash"] != episode["experience_hash"]
            or experience["payload"] != snapshot
            or parent["version_hash"] != episode["parent_version_hash"]
        ):
            raise ValueError("原经验、父版或冻结模型输入已不一致")
        events = [
            (row[0], _unseal(row[1], "event_hash"))
            for row in db.execute(
                "SELECT seq,payload FROM creative_harness_events WHERE run_id=? ORDER BY seq",
                (trace_run_id,),
            )
        ]
        finals = [
            (seq, event)
            for seq, event in events
            if event["type"] == "model/result"
            and event["payload"].get("request_id") == requests[-1]["id"]
            and event["payload"].get("status") == "done"
            and not event["payload"].get("response", {}).get("tool_calls")
        ]
        if len(finals) != 1:
            raise ValueError("原Reflector没有唯一已完成的最终报告")
        if not any(
            event["type"] == "tool/result"
            and event["payload"].get("name") == "read_feedback_evidence"
            and event["payload"].get("is_error") is False
            and event["payload"].get("value", {}).get("snapshot_hash") == content_hash(snapshot)
            for _, event in events
        ):
            raise ValueError("没有成功消费同一冻结经验的真实工具结果")
    trace = store.get_run(trace_run_id)
    task = store.get_task(owner.task_id)
    if trace.task_id != task.id or trace.revision != task.revision or task.project != project:
        raise ValueError("原轨迹任务版本已过期")
    source = v2_detail(store, project, snapshot["run_id"])
    _check_references(root, source["references"])
    if source.get("collaboration", {}).get("policy", {}).get("allow_read_tools") is False:
        raise ValueError("源证据使用权限已撤销")
    refs = [SourceRef.model_validate(value) for value in context["allowed_source_refs"]]
    normalization = normalize_reflection_report(
        finals[0][1]["payload"]["response"]["text"],
        snapshot,
        refs,
        author_feedback=experience["kind"] == "author_feedback",
    )
    raw_request_hash = requests[-1]["request_hash"]
    key = content_hash([project, episode_id, trace_run_id, requests[-1]["id"], raw_request_hash])
    return {
        "status": "legacy_read_only",
        "new_dispatch": False,
        "continuation_key": key,
        "episode": episode,
        "original_result_hash": result["result_hash"],
        "original_trace_run_id": trace_run_id,
        "raw_request_id": requests[-1]["id"],
        "raw_request_hash": raw_request_hash,
        "response_event_seq": finals[0][0],
        "owner": owner.model_dump(),
        "owner_identity": owner.identity,
        "budget_id": requests[0]["budget_id"],
        "budget_attempts": attempts,
        "historical_dispatch_cap": 3,
        "current_budget_policy": "use_BudgetLedger",
        "normalization": normalization,
        "allowed_source_refs": [r.model_dump(mode="json") for r in refs],
        "snapshot": snapshot,
        "source_ref": experience["source_ref"],
        "parent": parent,
        "author_feedback": experience["kind"] == "author_feedback",
    }


def continue_reflection_curator(
    store,
    root,
    project,
    episode_id,
    trace_run_id,
    *,
    expected_raw_request_hash,
    expected_normalization_hash,
    provider=None,
    embedder=None,
    emit=None,
) -> dict[str, Any]:
    """Historical entry point is intentionally read-only after policy migration.

    The original third-dispatch operation and failed receipt are retained. New
    work must be an explicit execution attempt on the same episode/owner through
    evolve_family(attempt_reason=...), governed by BudgetLedger. Replaying this
    older entry point could otherwise recreate its superseded fixed-cap claim.
    """
    raise ValueError(
        "旧第三次专用续接仅保留读取；请按原episode谱系使用evolve_family的attempt_reason，"
        "由持久BudgetLedger准入；未知claim须先对账，不能重放旧续接"
    )


def record_legacy_graph_validation(
    store,
    project,
    episode_id,
    source_request_id,
    *,
    expected_raw_request_hash,
) -> dict[str, Any]:
    """Validate the REAL saved Curator business edits in an independent new receipt.

    No edited proposal argument is accepted: request and response come from the
    durable Harness records. No model is called, old status is never rewritten,
    and a compiled graph here is not an installed or executed candidate version.
    """
    from .graph_delta import compile_graph_edit, project_legacy_graph_delta
    from .graph_v2 import PortSpec, narrative_role_registry
    from .harness import HarnessStore
    from .v2_skills import NarrativeEvolutionStore

    skills = NarrativeEvolutionStore(store)
    episode = skills.get_episode(project, episode_id)
    original_result = episode["result"]
    if original_result is None or original_result["status"] != "failed":
        raise ValueError("独立旧提议验证只读取已有不可变failed来源")
    request = HarnessStore(store).reconstruct_request(source_request_id)
    owner = BudgetOwner.model_validate(request["owner"])
    if (
        request["request_hash"] != expected_raw_request_hash
        or request["request_hash"] != content_hash(request["request"])
        or owner.project != project
        or owner.product_key != "method-evolution/" + episode["family_id"]
        or owner.problem_key != episode["budget_owner"]
        or request["request"]["role_id"] != "method_curator"
    ):
        raise ValueError("原提议请求hash／角色／稳定owner与原episode不一致")
    events = HarnessStore(store).events(request["run_id"])
    completed = [
        event
        for event in events
        if event["type"] == "model/result"
        and event["payload"].get("request_id") == source_request_id
        and event["payload"].get("status") == "done"
    ]
    if len(completed) != 1:
        raise ValueError("原Curator没有唯一已知完成响应，未知请求不能被新验证代替对账")
    response_text = completed[0]["payload"]["response"]["text"]
    proposal = _parse_report(response_text)
    parent = skills.get_version(project, episode["parent_version_id"])
    if parent["version_hash"] != episode["parent_version_hash"]:
        raise ValueError("旧提议冻结父版已不一致")
    identity = content_hash(
        [
            "legacy-graph-validation-v2",
            project,
            episode_id,
            source_request_id,
            expected_raw_request_hash,
            content_hash(response_text),
            parent["version_hash"],
        ]
    )
    payload = {
        "id": identity,
        "protocol": "legacy-graph-validation-v2",
        "project": project,
        "episode_id": episode_id,
        "problem_id": episode["budget_owner"],
        "original_result_hash": original_result["result_hash"],
        "source_request_id": source_request_id,
        "raw_request_hash": expected_raw_request_hash,
        "raw_response_text": response_text,
        "raw_response_hash": content_hash(response_text),
        "raw_proposal": proposal,
        "raw_proposal_hash": content_hash(proposal),
        "parent_version_hash": parent["version_hash"],
        "legacy_target_description": proposal.get("target_problem"),
        "old_status": "unchanged",
        "candidate_execution": "not_performed",
        "candidate_installation": "not_performed",
        "grounding": "not_rejudged",
        "author_acceptance": "unknown",
        "created_at": now(),
    }
    try:
        projection = project_legacy_graph_delta(proposal["graph_delta"])
        compiled = compile_graph_edit(
            parent["graph"],
            projection["edit"],
            narrative_role_registry(),
            {"task": PortSpec(type_name="task_context")},
        )
        payload.update(
            status="graph_compiled",
            projection=projection,
            compilation=compiled.model_dump(mode="json"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        payload.update(status="validation_failed", error=f"{type(exc).__name__}: {exc}")
    receipt = {**payload, "receipt_hash": content_hash(payload)}
    with store.connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS evolution_graph_revalidations("
            "id TEXT PRIMARY KEY,project TEXT NOT NULL,payload TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT payload FROM evolution_graph_revalidations WHERE id=? AND project=?",
            (identity, project),
        ).fetchone()
        if old:
            return _unseal(old[0], "receipt_hash")
        db.execute(
            "INSERT INTO evolution_graph_revalidations VALUES(?,?,?)",
            (identity, project, _json(receipt)),
        )
    return receipt
