"""Grounded Reflector -> Curator production episode under one Harness budget."""

from __future__ import annotations

import json
import os

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..content.hash import content_hash
from ..llm.execution_policy import SINGLE_DISPATCH_POLICY
from ..llm.resilience import build_real_provider
from .evolution import GuidanceEntry, SourceRef
from .evolution_v2 import (
    ReflectionDraftV3,
    ReusableEntry,
    SemanticMaintenance,
    TypedEvolutionDraftV3,
    TypedEvolutionProposalV2,
    bind_method_schema,
    compile_evolution_draft,
    compile_reflection_draft,
    install_entry_templates,
    verify_grounding,
    verify_problem_identity,
)
from .graph_delta import graph_edit_context
from .graph_v2 import GraphSpecV2, narrative_role_registry
from .harness import BudgetOwner, Harness, HarnessStore
from .models import RunRecord
from .reflection_continuation_v2 import available_parameter_bindings, bound_parameter_schema


class _ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str


class _ReadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_json: str
    snapshot_hash: str


def _episode_with_attempt(episode, state):
    """Expose the latest separate outcome without mutating the historical result."""
    result = episode["result"]
    latest = state.get("latest") or {}
    if latest.get("status") in {"completed", "completed_failed"}:
        evidence = (latest.get("events") or [{}])[-1].get("evidence", {})
        if evidence.get("status") in {"candidate", "failed"}:
            result = evidence
    return {
        **episode,
        "original_result": episode["result"],
        "result": result,
        "attempt_state": state,
        "new_proposal_dispatched": False,
    }


def build_evidence_capability(
    store, project, experience_id, snapshot, *, execution_attempt_id=None
):
    from .capabilities_v2 import (
        AdapterCatalog,
        CapabilityDescriptor,
        CapabilityPolicy,
        CapabilityStore,
    )

    input_model = create_model(
        "ReadCurrentMethodEvidence",
        __base__=_ReadInput,
        evidence_id=(str, Field(..., json_schema_extra={"enum": [experience_id]})),
    )

    def read_evidence(value):
        if value["evidence_id"] != experience_id:
            raise ValueError("只能读取本次授权经验")
        return {
            "evidence_json": json.dumps(snapshot, ensure_ascii=False),
            "snapshot_hash": content_hash(snapshot),
        }

    catalog = AdapterCatalog()
    capability_id = "method_evidence_" + experience_id
    if execution_attempt_id:
        capability_id += "_" + execution_attempt_id
    scope = "experience:" + experience_id
    binding = catalog.register_python(
        capability_id,
        project=project,
        input_model=input_model,
        output_model=_ReadOutput,
        execute=read_evidence,
        roles={"method_reflector"},
        read_scopes={scope},
    )
    descriptor = CapabilityDescriptor(
        project=project,
        id=capability_id,
        tool_name="read_feedback_evidence",
        description="读取本episode已授权的冻结经验；evidence_json包含原始JSON，snapshot_hash可核对",
        input_schema=input_model.model_json_schema(),
        output_schema=_ReadOutput.model_json_schema(),
        roles=("method_reflector",),
        read_scopes=(scope,),
        adapter_binding=binding,
        source_ref={
            "experience_id": experience_id,
            "snapshot_hash": content_hash(snapshot),
            "execution_attempt_id": execution_attempt_id,
        },
    )
    policy = CapabilityPolicy(
        project=project,
        allowed_roles=("method_reflector",),
        allowed_read_scopes=(scope,),
        preauthorized_descriptor_hashes=(descriptor.descriptor_hash,),
    )
    capabilities = CapabilityStore(store, catalog)
    record = capabilities.discover(project, descriptor)
    record = capabilities.validate(
        project, capability_id, "1", expected_hash=record["record_hash"], policy=policy
    )
    if record["descriptor"]["status"] != "validated":
        raise ValueError("冻结证据能力未通过正式合同核验：" + str(record["validation_errors"]))
    record = capabilities.enable(
        project,
        capability_id,
        "1",
        expected_hash=record["record_hash"],
        policy=policy,
        authority="preauthorized",
    )
    return capabilities, policy, record


def _curate(
    skills,
    project,
    parent,
    proposal,
    semantic,
    source_ref,
    expected_family_hash,
    *,
    model_proposal=None,
    entry_compilation=None,
):
    from .v2_skills import compile_business_edit

    graph = parent["graph"]
    if not isinstance(proposal, TypedEvolutionProposalV2):
        raise ValueError("旧proposal只兼容读取／独立验证，不能作为新版本写入协议")
    graph_receipt = None
    if proposal.graph_edit is not None:
        graph_receipt = compile_business_edit(graph, proposal.graph_edit.model_dump(mode="json"))
        graph = graph_receipt["applied"]["graph"]
    old = {row["id"]: GuidanceEntry.model_validate(row) for row in parent["entries"]}
    updated = {row.id: row for row in proposal.upsert_entries}
    if proposal.conditions is not None:
        proposed_conditions = proposal.conditions.model_dump(mode="json")
        for key in ("required_resources", "required_capabilities", "free_text"):
            if not set(parent["conditions"][key]) <= set(proposed_conditions[key]):
                raise ValueError("演化不能删除父版适用限制，放宽需独立确认")
        for requirement in parent["conditions"]["requirements"]:
            if requirement not in proposed_conditions["requirements"]:
                raise ValueError("演化不能削弱父版机器条件")
    for entry_id, replacement in updated.items():
        if (
            entry_id in old
            and old[entry_id].evidence_kind == "author_preference"
            and (
                replacement.guidance().model_dump(mode="json")
                != old[entry_id].model_dump(mode="json")
            )
        ):
            raise ValueError("方法演化不能静默改写已记录作者偏好")
    retired = list(proposal.remove_entry_ids)
    pairs = {frozenset({row["left_id"], row["right_id"]}) for row in semantic.get("pairs", [])}
    templates = {
        row["id"]: ReusableEntry.model_validate(row)
        for node in parent["graph"]["nodes"]
        for row in node.get("parameters", {}).get("skill_entry_templates", [])
    }
    for merge in proposal.merges:
        if (
            semantic["status"] != "encoded"
            or frozenset({merge.keep_id, merge.remove_id}) not in pairs
            or merge.keep_id not in updated
            or merge.remove_id not in old
            or merge.keep_id == merge.remove_id
        ):
            raise ValueError("语义合并必须引用真实近邻及显式保留条目")
        kept = updated[merge.keep_id]
        removed = old[merge.remove_id]
        if kept.operation != removed.operation or kept.evidence_kind != removed.evidence_kind:
            raise ValueError("不能跨角色或证据性质合并经验")
        previous = templates.get(merge.remove_id)
        if previous is not None:
            if [(s.name, s.kind, s.binding_key) for s in kept.slots] != [
                (s.name, s.kind, s.binding_key) for s in previous.slots
            ]:
                raise ValueError("适用参数不同的经验不能以相似度强行合并")
            kept = kept.model_copy(
                update={
                    "support_refs": list(
                        {
                            content_hash(r): r for r in [*kept.support_refs, *previous.support_refs]
                        }.values()
                    ),
                    "counterexample_refs": list(
                        {
                            content_hash(r): r
                            for r in [*kept.counterexample_refs, *previous.counterexample_refs]
                        }.values()
                    ),
                }
            )
            updated[kept.id] = kept
        retired.append(merge.remove_id)
    if len(retired) != len(set(retired)) or set(retired) & set(updated):
        raise ValueError("条目不能重复删除或同时保留和删除")
    for entry_id in retired:
        if entry_id not in old:
            raise ValueError("不能移除不存在的经验条目")
        if old[entry_id].evidence_kind == "author_preference":
            raise ValueError("方法演化不能自行撤销作者偏好")
        del old[entry_id]
    for entry in updated.values():
        guidance = entry.guidance()
        inherited = []
        for merge in proposal.merges:
            if merge.keep_id == entry.id:
                removed = next(row for row in parent["entries"] if row["id"] == merge.remove_id)
                inherited.extend(SourceRef.model_validate(ref) for ref in removed["source_refs"])
        guidance.source_refs = list(
            {content_hash(ref): ref for ref in [*guidance.source_refs, *inherited]}.values()
        )
        old[entry.id] = guidance
    graph = install_entry_templates(graph, list(updated.values()), retired)
    return skills.add_version(
        project,
        parent["family_id"],
        graph=graph,
        entries=list(old.values()),
        conditions=proposal.conditions or parent["conditions"],
        description=proposal.description
        if proposal.description is not None
        else parent["description"],
        parent_version_id=parent["id"],
        expected_family_hash=expected_family_hash,
        source_refs=[source_ref],
        delta={
            "proposal": proposal.model_dump(mode="json"),
            "model_proposal": model_proposal,
            "entry_compilation": entry_compilation,
            "source_run_id": source_ref.id,
            "semantic_maintenance": semantic,
            "graph_compilation": graph_receipt,
        },
    )


def evolve_family(
    store,
    root,
    project,
    family_id,
    source_run_id,
    text,
    *,
    emit=None,
    provider=None,
    source_kind="author_feedback",
    embedder=None,
    attempt_reason=None,
):
    from .budget_policy import BudgetLedger
    from .v2_service import _check_references, _json, v2_detail
    from .v2_skills import NarrativeEvolutionStore

    source = v2_detail(store, project, source_run_id)
    if source.get("collaboration", {}).get("policy", {}).get("allow_read_tools") is False:
        HarnessStore(store).append(
            source_run_id,
            "evolution/admission_rejected",
            {"reason": "author_policy_disallows_read_tools", "model_dispatches": 0},
        )
        raise ValueError("当前作者策略未允许读取工具，停止演化且不派发模型")
    task = store.get_task(source["task_id"])
    skills = NarrativeEvolutionStore(store)
    parent_id = source.get("skill_version_id") or (source.get("saved_skill") or {}).get(
        "version_id"
    )
    if not parent_id or not any(
        row["id"] == parent_id for row in skills.versions(project, family_id)
    ):
        raise ValueError("源运行没有采用或创建该技能家族")
    parent = skills.get_version(project, parent_id)
    family = skills.get_family(project, family_id)
    outcomes = (source.get("result") or {}).get("outcomes", {})
    errors = " ".join(str(row.get("error", "")) for row in outcomes.values()).lower()
    operational = any(
        token in errors for token in ("connection", "timeout", "401", "429", "apierror", "network")
    )
    requirements = (source.get("requirements") or {}).get("machine") or {}
    snapshot = {
        "run_id": source_run_id,
        "graph": source["graph"],
        "outcomes": outcomes,
        "report": (source.get("result") or {}).get("outputs", {}).get("report"),
        "feedback": text,
        "task_feedback": task.feedback,
        "original_brief": task.request.brief,
        "protected": task.request.protected,
        "feedback_origin": source_kind,
        "entities": task.base_bundle.get("entities", {}),
        "machine_requirements": requirements,
    }
    source_ref = SourceRef(
        kind="development_input"
        if source_kind == "development_input"
        else "feedback"
        if text.strip()
        else "run",
        id=source_run_id,
        content_hash=content_hash(snapshot),
    )
    experience = skills.record_experience(
        project,
        family_id,
        kind="provider_failure"
        if operational
        else "hypothesis"
        if source_kind == "development_input"
        else "author_feedback"
        if text.strip()
        else "technical_failure",
        source_ref=source_ref,
        payload=snapshot,
        problem_key="narrative-method",
        version_id=parent_id,
    )
    if operational:
        return {
            "status": "operational_failure_recorded",
            "experience": experience,
            "candidate": None,
        }
    episode = skills.begin_episode(
        project,
        family_id,
        experience_id=experience["id"],
        problem_key="narrative-method",
        parent_version_id=parent_id,
    )
    episode = skills.get_episode(project, episode["id"])
    ledger = BudgetLedger(store)
    operation_key = "method-episode:" + episode["id"]
    owner = BudgetOwner(
        project=project,
        task_id=task.id,
        product_key="method-evolution/" + family_id,
        problem_key=episode["budget_owner"],
    )
    problem_id = episode["budget_owner"]
    original_result = episode["result"]
    state = ledger.execution_state(operation_key)
    if state["status"] == "completed":
        return _episode_with_attempt(episode, state)
    if original_result is not None and not attempt_reason:
        return _episode_with_attempt(episode, state)
    if state["status"] == "completed_failed" and not attempt_reason:
        return _episode_with_attempt(episode, state)
    with store.connection() as db:
        old_claim = db.execute(
            "SELECT 1 FROM evolution_episode_claims WHERE episode_id=? AND project=?",
            (episode["id"], project),
        ).fetchone()
    legacy_state = (
        "completed_failed"
        if original_result and original_result["status"] != "candidate"
        else "completed"
        if original_result
        else "claimed_unknown"
        if old_claim
        else None
    )
    attempt = ledger.claim_execution(
        operation_key,
        owner.identity,
        content_hash(
            {
                "protocol": "method-proposal-v2",
                "source_hash": source_ref.content_hash,
                "parent_version_hash": parent["version_hash"],
                "problem_id": problem_id,
            }
        ),
        reason=attempt_reason or "首次执行宿主合同化Reflector／Curator生产流程",
        legacy_state=legacy_state,
    )
    if attempt.get("status") != "claimed_unknown" or attempt.get("new_dispatch") is not True:
        return _episode_with_attempt(episode, ledger.execution_state(operation_key))
    # A repeated unknown claim is never permission to send the model again.
    if state["status"] == "claimed_unknown" or legacy_state == "claimed_unknown":
        return {
            **episode,
            "attempt_state": ledger.execution_state(operation_key),
            "status": "in_progress_or_unknown",
            "new_proposal_dispatched": False,
        }
    owned = provider is None
    trace = None
    trace_persisted = False
    capabilities = None
    capability_record = None
    events = HarnessStore(store)
    try:
        provider = provider or build_real_provider(
            model=task.request.model or os.getenv("OWCOPILOT_CHEAP_MODEL") or "deepseek-v4-flash",
            json_mode=False,
            timeout=120,
            max_retries=0,
            max_output_tokens=6500,
            execution_policy=SINGLE_DISPATCH_POLICY,
        )
        trace = RunRecord(task_id=task.id, revision=task.revision, status="running")
        with store.connection() as db:
            db.execute(
                "INSERT INTO runs VALUES(?,?,?)", (trace.id, task.id, trace.model_dump_json())
            )
        trace_persisted = True

        def cancelled():
            current_task = store.get_task(task.id)
            if current_task.revision != task.revision:
                return True
            try:
                _check_references(root, source["references"])
            except (ValueError, OSError):
                return True
            if source.get("collaboration"):
                from .collaboration_v2 import CollaborationStore

                current = CollaborationStore(store).detail(project, source_run_id)
                return current["policy"].get("allow_read_tools") is False
            return False

        harness = Harness(events, trace.id, provider, cancelled=cancelled)
        history = skills.experiences(project, family_id)
        refs = {content_hash(source_ref): source_ref}
        for row in history:
            ref = SourceRef.model_validate(row["source_ref"])
            refs[content_hash(ref)] = ref
        for entry in parent["entries"]:
            for raw in entry["source_refs"]:
                ref = SourceRef.model_validate(raw)
                refs[content_hash(ref)] = ref
        capabilities, tool_policy, capability_record = build_evidence_capability(
            store, project, experience["id"], snapshot, execution_attempt_id=attempt["id"]
        )
        reflected = capabilities.run_tools(
            harness,
            owner=owner,
            node_id="method_reflector",
            role_id="method_reflector",
            policy=tool_policy,
            max_dispatches=2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是Reflector，仅诊断实际轨迹和抽象可复用经验，不输出最终GraphDelta。"
                        f"先调用read_feedback_evidence，evidence_id={experience['id']}，"
                        "再输出ReflectionDraftV3 JSON。problem_id逐字采用宿主固定ID；"
                        "target_description是可自由表述的诊断描述，不是身份。"
                        "每条经验使用EntryTemplateIR.parts分段：文字用{kind:text,text:...}；"
                        "参数用{kind:binding,binding_kind:entity或requirement,binding_key:...}，"
                        "binding_kind与binding_key从available_parameter_bindings同一行选择。"
                        "不要写text/slots字段，不手写{{占位符}}、槽名或source_value；宿主生成这些派生字段。"
                        "operation必须单个注册角色名，跨角色通用经验用空字符串，不能斜杠拼接多个角色。"
                        "只引用给定SourceRef。实例实体／数量要求需要抽象时必须用binding片段，"
                        "区分支持与反例、观察与假设，不把开发输入称作者偏好。不要改作者原题。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source": snapshot,
                            "parent": parent,
                            "problem_id": problem_id,
                            "history": history,
                            "available_parameter_bindings": available_parameter_bindings(snapshot),
                            "allowed_source_refs": [
                                ref.model_dump(mode="json") for ref in refs.values()
                            ],
                            "output_schema": bind_method_schema(
                                bound_parameter_schema(ReflectionDraftV3, snapshot),
                                problem_id=problem_id,
                                registry=narrative_role_registry(),
                            ),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            context_manifest={
                "episode_id": episode["id"],
                "source_hash": source_ref.content_hash,
                "role": "reflector",
                "execution_attempt_id": attempt["id"],
                "budget_policy": attempt.get("policy"),
            },
        )
        if reflected["status"] != "completed":
            raise ValueError("Reflector未在预留预算内形成报告：" + reflected["status"])
        if not any(
            event["type"] == "tool/result"
            and event["payload"].get("name") == "read_feedback_evidence"
            and event["payload"].get("is_error") is False
            for event in events.events(trace.id)
        ):
            raise ValueError("Reflector未实际成功读取授权证据，不能以文本说明冒充工具调用")
        capabilities.revoke(
            project,
            capability_record["descriptor"]["id"],
            "1",
            expected_hash=capabilities.get(project, capability_record["descriptor"]["id"])[
                "record_hash"
            ],
            reason="本episode反思读取阶段结束，停止后续消费",
        )
        reflection_draft = ReflectionDraftV3.model_validate(_json(reflected["text"]))
        report, reflection_compilation = compile_reflection_draft(
            reflection_draft, snapshot, narrative_role_registry()
        )
        verify_problem_identity(problem_id, report)
        verify_grounding(
            report,
            None,
            list(refs.values()),
            set(snapshot["entities"]),
            requirements,
            author_feedback=experience["kind"] == "author_feedback",
        )
        events.append(
            trace.id,
            "evolution/reflection",
            {
                "report": report.model_dump(mode="json"),
                "model_draft": reflection_draft.model_dump(mode="json"),
                "entry_compilation": reflection_compilation,
            },
            node_id="method_reflector",
        )
        combined = {row["id"]: GuidanceEntry.model_validate(row) for row in parent["entries"]}
        combined.update({entry.id: entry.guidance() for entry in report.reusable_entries})
        try:
            semantic = SemanticMaintenance(store, embedder).candidates(
                project, family_id, list(combined.values())
            )
        except Exception as exc:
            semantic = {
                "status": "unavailable",
                "pairs": [],
                "error": str(exc),
                "fallback": "forbidden",
            }
        events.append(trace.id, "evolution/semantic_maintenance", semantic)
        curated = harness.complete(
            owner=owner,
            node_id="method_curator",
            role_id="method_curator",
            system=(
                "你是Curator，只整理Reflector诊断，提出唯一TypedEvolutionDraftV3 JSON。"
                "problem_id逐字采用宿主固定ID；target_description可表述或翻译，不能当身份。"
                "graph_edit必须是明确节点/边patch，不准给variant标签。expected_graph_hash必须原样采用。"
                "只用注册能力；insert_registered_role只给node_id/operation/source_node/guidance，"
                "不得生成预算product/problem、role_version、context_bindings、parameters或权限字段。"
                "这些由宿主从已注册角色及合法原source编译；必需门禁和产物不削弱。"
                "需要结构变化才填graph_edit；仅指导变更必须分类guidance。"
                "经验upsert_entries使用parts分段（text或binding），不要写text/slots/source_value；"
                "binding_kind/binding_key选择冻结表同一行，宿主编译模板和槽名。"
                "operation为单个注册角色或空字符串表示通用，不能多角色拼接。"
                "保留支持与反例来源；去重只可用真实semantic pairs且必须保留被合并来源。"
                "不代作者接受，不把文学意图或模型诊断称已验证；条目参数不能保留源实例值。"
            ),
            user=json.dumps(
                {
                    "parent": {
                        key: parent[key]
                        for key in (
                            "id",
                            "version",
                            "version_hash",
                            "entries",
                            "conditions",
                            "description",
                        )
                    },
                    "graph": graph_edit_context(
                        GraphSpecV2.model_validate(parent["graph"]), narrative_role_registry()
                    ),
                    "problem_id": problem_id,
                    "reflection": reflection_draft.model_dump(mode="json"),
                    "compiled_reflection_hash": content_hash(report),
                    "allowed_source_refs": [ref.model_dump(mode="json") for ref in refs.values()],
                    "semantic": semantic,
                    "available_parameter_bindings": available_parameter_bindings(snapshot),
                    "expected_graph_hash": parent["execution_graph_hash"],
                    "registered_roles": {
                        key: value.model_dump(mode="json")
                        for key, value in narrative_role_registry().items()
                    },
                    "output_schema": bind_method_schema(
                        bound_parameter_schema(TypedEvolutionDraftV3, snapshot),
                        problem_id=problem_id,
                        graph=GraphSpecV2.model_validate(parent["graph"]),
                        registry=narrative_role_registry(),
                        graph_hash=parent["execution_graph_hash"],
                    ),
                },
                ensure_ascii=False,
            ),
            context_manifest={
                "episode_id": episode["id"],
                "reflection_hash": content_hash(report),
                "parent_version_hash": parent["version_hash"],
                "role": "curator",
                "execution_attempt_id": attempt["id"],
                "budget_policy": attempt.get("policy"),
                "candidate_limit": 1,
            },
        )
        proposal_draft = TypedEvolutionDraftV3.model_validate(_json(curated))
        proposal, entry_compilation = compile_evolution_draft(
            proposal_draft, snapshot, narrative_role_registry()
        )
        verify_problem_identity(problem_id, report, proposal)
        verify_grounding(
            report,
            proposal,
            list(refs.values()),
            set(snapshot["entities"]),
            requirements,
            author_feedback=experience["kind"] == "author_feedback",
        )
        if cancelled():
            raise ValueError("源任务、证据或许可已变化，迟到提议不发布")
        candidate = _curate(
            skills,
            project,
            parent,
            proposal,
            semantic,
            source_ref,
            family["family_hash"],
            model_proposal=proposal_draft.model_dump(mode="json"),
            entry_compilation=entry_compilation,
        )
        outcome = {
            "status": "candidate",
            "candidate_version_id": candidate["id"],
            "evidence": {
                "candidate_execution": "not_performed",
                "author_acceptance": "unknown",
                "reflection_hash": content_hash(report),
                "change_kind": proposal.change_kind,
                "semantic_status": semantic["status"],
                "source_run_id": source_run_id,
            },
        }
        if original_result is None:
            skills.finish_episode(
                project,
                episode["id"],
                status="candidate",
                candidate_version_id=candidate["id"],
                evidence={
                    "candidate_execution": "not_performed",
                    "author_acceptance": "unknown",
                    "reflection_hash": content_hash(report),
                    "change_kind": proposal.change_kind,
                    "semantic_status": semantic["status"],
                    "source_run_id": source_run_id,
                },
            )
        ledger.finish_execution(attempt["id"], status="completed", evidence=outcome)
        store.update_run(trace.id, status="completed")
    except Exception as exc:
        outcome = {
            "status": "failed",
            "candidate_version_id": None,
            "evidence": {
                "error": f"{type(exc).__name__}: {exc}",
                "candidate_execution": "not_performed",
                "author_acceptance": "unknown",
            },
        }
        if original_result is None:
            skills.finish_episode(
                project, episode["id"], status="failed", evidence=outcome["evidence"]
            )
        unresolved = []
        if trace is not None and trace_persisted:
            for budget_id in {row["budget_id"] for row in events.requests(trace.id)}:
                unresolved.extend(
                    row
                    for row in ledger.dispatches(budget_id)
                    if row.get("run_id") == trace.id and row["status"] == "claimed_unknown"
                )
        if unresolved:
            # A transport exception is not evidence that the remote call did not run.
            # Retain the execution claim for explicit reconciliation before retry.
            outcome["evidence"]["dispatch_state"] = "claimed_unknown"
            outcome["evidence"]["unresolved_dispatch_ids"] = [row["id"] for row in unresolved]
        else:
            ledger.finish_execution(attempt["id"], status="completed_failed", evidence=outcome)
        if trace is not None and trace_persisted:
            store.update_run(trace.id, status="failed", error=str(exc))
    finally:
        if capabilities is not None and capability_record is not None:
            current = capabilities.get(project, capability_record["descriptor"]["id"])
            if current["descriptor"]["status"] == "enabled":
                capabilities.revoke(
                    project,
                    current["descriptor"]["id"],
                    "1",
                    expected_hash=current["record_hash"],
                    reason="episode退出，撤销临时证据读取许可",
                )
        if owned and provider is not None and getattr(provider, "close", None):
            provider.close()
    result = skills.get_episode(project, episode["id"])
    if trace is not None and trace_persisted:
        events.append(
            trace.id,
            "evolution/result",
            {
                "episode_id": episode["id"],
                "result": outcome,
                "original_result": result["result"],
                "execution_attempt_id": attempt["id"],
                "trial": "requires_separate_permission",
            },
        )
        if emit:
            emit("evolution/result", {"episode_id": episode["id"], "trace_run_id": trace.id})
    return {
        **result,
        "original_result": result["result"],
        "result": outcome,
        "attempt_state": ledger.execution_state(operation_key),
        "trace_run_id": trace.id if trace else None,
    }
