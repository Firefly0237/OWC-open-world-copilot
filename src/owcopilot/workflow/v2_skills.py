"""Narrative host policy: bounded topology rewrites and genuinely consumed skill deltas."""

from __future__ import annotations

import json
import sqlite3
import threading

from ..content.hash import content_hash
from ..storage.sqlite import build_fts_match_query, cjk_fts_text
from .evolution import (
    EvolutionStore,
    ExecutionEvidence,
    GuidanceEntry,
    SkillConditions,
    SourceRef,
    assess_conditions,
)
from .graph_v2 import (
    GraphSpecV2,
    PortSpec,
    compile_graph,
    narrative_role_registry,
)

_SAVE_LOCK = threading.Lock()
CAPABILITIES = {
    "event_plan",
    "quest",
    "dialogue_tree",
    "quest_logic",
    "decision_effect_map",
    "choice_map",
    "coverage_audit",
}


def _compile(graph):
    return compile_graph(
        GraphSpecV2.model_validate(graph),
        narrative_role_registry(),
        {"task": PortSpec(type_name="task_context")},
    )


def _materialize(graph, entries):
    spec = GraphSpecV2.model_validate(graph)
    nodes = []
    for node in spec.nodes:
        parameters = dict(node.parameters)
        original = parameters.get("skill_base_guidance", node.guidance)
        selected = [
            item.model_dump(mode="json")
            for item in entries
            if item.operation in {"", node.operation}
        ]
        parameters.update(skill_base_guidance=original, skill_entries_materialized=True)
        guidance = original + (
            "\n有来源的技能条目：" + json.dumps(selected, ensure_ascii=False) if selected else ""
        )
        # Identical empty materialization must not change the baseline execution signature/hash.
        if not selected and not node.parameters.get("skill_entries_materialized"):
            nodes.append(node)
        else:
            nodes.append(node.model_copy(update={"guidance": guidance, "parameters": parameters}))
    return spec.model_copy(update={"nodes": nodes}).model_dump(mode="json")


class NarrativeEvolutionStore(EvolutionStore):
    def __init__(self, workflow):
        super().__init__(workflow, _compile)

    def add_version(self, project, family_id, *, graph, entries=None, **kwargs):
        entries = [GuidanceEntry.model_validate(item) for item in (entries or [])]
        return super().add_version(
            project, family_id, graph=_materialize(graph, entries), entries=entries, **kwargs
        )

    def match(self, project, query, *, limit=5, **kwargs):
        # Kernel supplies hard three-valued admission, then this host supplies real FTS5 ranking.
        candidates = super().match(project, query, limit=20, **kwargs)
        # A workflow request is a long paraphrase, not an exact source quotation.
        # FTS5 phrases require token adjacency (§3.2); tokenize CJK first so the
        # safe builder emits an OR of bigrams instead of whole-sentence phrases.
        terms = build_fts_match_query(cjk_fts_text(query))
        if not terms or not candidates:
            return candidates[:limit]
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE methods USING fts5(id UNINDEXED, title, body)")
            for row in candidates:
                family = self.get_family(project, row["family_id"])
                version = row["version"]
                db.execute(
                    "INSERT INTO methods VALUES(?,?,?)",
                    (
                        row["version_id"],
                        cjk_fts_text(family["name"]),
                        cjk_fts_text(
                            " ".join(
                                [
                                    family["description"],
                                    version["description"],
                                    *(item["text"] for item in version["entries"]),
                                    *(node["operation"] for node in version["graph"]["nodes"]),
                                ]
                            )
                        ),
                    ),
                )
            ranked = db.execute(
                "SELECT id, bm25(methods,0,4,1) FROM methods "
                "WHERE methods MATCH ? ORDER BY bm25(methods,0,4,1)",
                (terms,),
            ).fetchall()
        scores = {row[0]: -float(row[1]) for row in ranked}
        for row in candidates:
            row["score"] = scores.get(row["version_id"], 0.0)
            row["retrieval"] = "sqlite_fts5_bm25_after_typed_admission"
            row["retrieval_query_mode"] = "cjk_bigram_disjunction"
        return sorted(candidates, key=lambda row: (not row["eligible_to_execute"], -row["score"]))[
            :limit
        ]


def bounded_rewrite(graph: dict, changes: dict) -> dict:
    """Legacy read/validation compatibility; new proposals use compile_business_edit."""
    from .graph_delta import apply_graph_delta

    return apply_graph_delta(
        graph, changes, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    ).graph.model_dump(mode="json")


def compile_business_edit(graph: dict, changes: dict) -> dict:
    """Return the full derivation receipt so version lineage retains host decisions."""
    from .graph_delta import compile_graph_edit

    return compile_graph_edit(
        graph, changes, narrative_role_registry(), {"task": PortSpec(type_name="task_context")}
    ).model_dump(mode="json")


def assert_skill_admission(
    store,
    task,
    skill_version_id,
    *,
    machine_requirements=None,
    trial_scope_id=None,
    expected_trial_scope_hash=None,
):
    current = store.get_task(task.id)
    if (current.project, current.revision, current.base_hash) != (
        task.project, task.revision, task.base_hash
    ):
        raise ValueError("流程准入所用任务版本已过期")
    skills = NarrativeEvolutionStore(store)
    version = skills.get_version(task.project, skill_version_id)
    family = skills.get_family(task.project, version["family_id"])
    state = version["state"]
    is_default = (
        family["default_version_id"] == version["id"] and state["author_acceptance"] == "accepted"
    )
    authorized = is_default or state["trial_status"] == "allowed"
    if family["disabled"] or state["author_acceptance"] == "rejected" or not authorized:
        raise ValueError("该方法尚未获得本次试用或默认采用许可")
    if not is_default or trial_scope_id or expected_trial_scope_hash:
        from .method_trial_scope_v2 import validate_trial_scope

        permit = validate_trial_scope(
            store, task, version["id"], trial_scope_id,
            expected_trial_scope_hash, machine_requirements or {},
        )
        if permit["allowed_versions"][version["id"]] != version["version_hash"]:
            raise ValueError("候选版本与试用许可不一致")
    assessment = assess_conditions(
        SkillConditions.model_validate(version["conditions"]),
        resources=set(task.base_bundle.get("entities", {})),
        capabilities=CAPABILITIES,
        requirements=_machine_values(machine_requirements),
        confirmed_requirement_keys=set(_machine_values(machine_requirements)),
    )
    if not assessment["compatible"]:
        raise ValueError("方法适用条件冲突或仍未知：" + json.dumps(assessment, ensure_ascii=False))
    from .evolution_v2 import render_parameterized_guidance

    for node in GraphSpecV2.model_validate(version["graph"]).nodes:
        render_parameterized_guidance(
            node,
            {
                "entities": list(task.base_bundle.get("entities", {}).values()),
                "machine_requirements": machine_requirements or {},
            },
        )
    return {
        "version": version,
        "is_default": is_default,
        "trial_scope_id": trial_scope_id,
        "trial_scope_hash": expected_trial_scope_hash,
        "authorization": {
            "selection": "default" if is_default else "trial",
            "state_hash": state["state_hash"],
            "version_hash": version["version_hash"],
            "default_version_id_at_selection": family["default_version_id"] if is_default else None,
        },
    }


def prepare_skill_run(
    store,
    root,
    task,
    skill_version_id,
    *,
    prior_run_id=None,
    plan_guidance=None,
    machine_requirements=None,
    collaboration_policy=None,
    narrative_project_id=None,
    narrative_scene_id=None,
    narrative_preview_id=None,
    narrative_transition_id=None,
    trial_scope_id=None,
    expected_trial_scope_hash=None,
):
    from .v2_service import prepare_v2_run

    admission = assert_skill_admission(
        store, task, skill_version_id, machine_requirements=machine_requirements,
        trial_scope_id=trial_scope_id, expected_trial_scope_hash=expected_trial_scope_hash,
    )
    version = admission["version"]
    return prepare_v2_run(
        store,
        root,
        task,
        version["graph"],
        prior_run_id=prior_run_id,
        skill_version_id=version["id"],
        guidance_entries=version["entries"],
        plan_guidance=plan_guidance,
        skill_authorization=admission["authorization"],
        machine_requirements=machine_requirements,
        collaboration_policy=collaboration_policy,
        narrative_project_id=narrative_project_id,
        narrative_scene_id=narrative_scene_id,
        narrative_preview_id=narrative_preview_id,
        narrative_transition_id=narrative_transition_id,
        method_trial_scope_id=admission["trial_scope_id"],
        method_trial_scope_hash=admission["trial_scope_hash"],
    )


def _machine_values(requirements):
    if requirements is None:
        return {}
    from .requirements_v2 import MachineRequirements

    value = MachineRequirements.model_validate(requirements).model_dump(exclude_none=True)
    return {
        key: item
        for key, item in value.items()
        if key != "schema_version" and type(item) in {str, int, bool}
    }


def check_run_authorization(store, project, run):
    from .method_trial_scope_v2 import assert_trial_scope_current

    assert_trial_scope_current(store, project, run)
    version_id = run.get("skill_version_id")
    if not version_id:
        return
    skills = NarrativeEvolutionStore(store)
    version = skills.get_version(project, version_id)
    family = skills.get_family(project, version["family_id"])
    state = version["state"]
    selected = run.get("skill_authorization") or {}
    if selected.get("selection") not in {"default", "trial"}:
        raise ValueError("方法运行缺少明确准入来源；历史记录可读但不能借此继续执行")
    if selected["selection"] == "default" and (
        selected.get("default_version_id_at_selection") != version_id
        or selected.get("version_hash") != version["version_hash"]
        or state["author_acceptance"] != "accepted"
    ):
        raise ValueError("默认流程缺少对应的作者采用与版本来源，需重新选择流程")
    if family["disabled"] or state["author_acceptance"] == "rejected":
        raise ValueError("技能已停用或被明确拒绝，停止后续派发与发布")
    if selected.get("selection") == "trial" and state["trial_status"] == "unlicensed":
        raise ValueError("本次方法试用许可已撤销")


def evaluate_skill_run(store, project, run_id, version_id):
    from .v2_service import v2_detail

    skills = NarrativeEvolutionStore(store)
    run = v2_detail(store, project, run_id)
    version = skills.get_version(project, version_id)
    result = run.get("result")
    if result is None:
        raise ValueError("尚无真实执行结果可评价")
    if result["graph_hash"] != version["execution_graph_hash"]:
        return {
            "status": "withheld",
            "reason": "当前任务额外指导改变执行图，不能归为原技能图的技术通过",
        }
    outcomes = result["outcomes"]
    report = result.get("outputs", {}).get("report", {})
    hard_errors = [row for row in report.get("issues", []) if row.get("severity") == "error"]
    evidence = ExecutionEvidence(
        run_id=run_id,
        graph_hash=result["graph_hash"],
        input_hash=content_hash([run["graph"], run["references"], run["task_id"], run["revision"]]),
        status=result["status"],
        consumed_artifact_hashes=[row["content_hash"] for row in result["artifacts"].values()],
        failed_nodes=[
            key for key, value in outcomes.items() if value["status"] not in {"succeeded", "reused"}
        ],
        checks={
            "bound_produced": "bound" in result["outputs"],
            "audit_present": bool(report),
            "no_hard_audit_errors": not hard_errors,
        },
    )
    comparison = None
    source_run_id = (version.get("delta") or {}).get("source_run_id")
    if source_run_id:
        from .evolution_v2 import compare_production_runs
        from .harness import HarnessStore

        harness = HarnessStore(store)
        comparison = compare_production_runs(
            v2_detail(store, project, source_run_id),
            run,
            harness.requests(source_run_id),
            harness.requests(run_id),
        )
    evaluation = skills.record_evaluation(
        project, version_id, evidence, comparison_evidence=comparison
    )
    skills.record_experience(
        project,
        version["family_id"],
        kind="success" if evaluation["technical_status"] == "passed" else "technical_failure",
        source_ref=SourceRef(kind="run", id=run_id, content_hash=content_hash(run)),
        payload={
            "checks": evidence.checks,
            "failed_nodes": evidence.failed_nodes,
            "semantic_status": "withheld",
            "comparison": comparison,
            "guidance_observations": [
                {
                    "entry_id": entry["id"],
                    "execution_outcome": evidence.status,
                    "support_or_counterexample": "supporting_execution"
                    if evaluation["technical_status"] == "passed"
                    else "counterexample_execution",
                    "attribution": "outcome_only_not_entry_causality",
                }
                for entry in version["entries"]
            ],
        },
        problem_key="narrative-production",
        version_id=version_id,
    )
    return evaluation


def save_run_skill(store, project, run_id, *, name):
    from .v2_service import v2_detail

    with _SAVE_LOCK:
        state = v2_detail(store, project, run_id)
        if not state.get("result"):
            raise ValueError("没有真实执行轨迹不能保存技能")
        skills = NarrativeEvolutionStore(store)
        saved = state.get("saved_skill")
        if saved:
            return {
                "family": skills.get_family(project, saved["family_id"]),
                "version": skills.get_version(project, saved["version_id"]),
            }
        family = skills.create_family(project, name, "从真实制作轨迹保存；文学效果与作者接受未知")
        version = skills.add_version(
            project,
            family["id"],
            graph=state["graph"],
            expected_family_hash=family["family_hash"],
            source_refs=[SourceRef(kind="run", id=run_id, content_hash=content_hash(state))],
        )
        evaluate_skill_run(store, project, run_id, version["id"])
        with store.connection() as db:
            state.pop("run", None)
            state["saved_skill"] = {"family_id": family["id"], "version_id": version["id"]}
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=? AND project=?",
                (json.dumps(state, ensure_ascii=False), run_id, project),
            )
        return {
            "family": skills.get_family(project, family["id"]),
            "version": skills.get_version(project, version["id"]),
        }


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
    from .v2_method_runtime import evolve_family as run_method_episode

    return run_method_episode(
        store,
        root,
        project,
        family_id,
        source_run_id,
        text,
        emit=emit,
        provider=provider,
        source_kind=source_kind,
        embedder=embedder,
        attempt_reason=attempt_reason,
    )


def method_episode_detail(store, project, episode_id):
    """Current attempt view plus the unchanged original episode result."""
    from .budget_policy import BudgetLedger
    from .v2_method_runtime import _episode_with_attempt

    skills = NarrativeEvolutionStore(store)
    episode = skills.get_episode(project, episode_id)
    state = BudgetLedger(store).execution_state("method-episode:" + episode_id)
    detail = _episode_with_attempt(episode, state)
    experience = next(
        row
        for row in skills.experiences(project, episode["family_id"])
        if row["id"] == episode["experience_id"]
    )
    detail["source"] = {
        "run_id": experience["payload"]["run_id"],
        "text": experience["payload"]["feedback"],
        "source_kind": experience["payload"].get("feedback_origin", "author_feedback"),
        "snapshot_hash": content_hash(experience["payload"]),
    }
    candidate_versions = {
        version["id"]: version
        for version in skills.versions(project, episode["family_id"])
        if version["parent_version_id"] == episode["parent_version_id"]
        and experience["source_ref"] in version["source_refs"]
    }
    with store.connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        trials = []
        if {"creative_v2_trial_claims_v2", "creative_v2_trial_results_v2"} <= tables:
            results = {
                row[0]: json.loads(row[1])
                for row in db.execute(
                    "SELECT trial_key,payload FROM creative_v2_trial_results_v2 WHERE project=?",
                    (project,),
                )
            }
            for row in db.execute(
                "SELECT trial_key,payload FROM creative_v2_trial_claims_v2 "
                "WHERE project=? ORDER BY rowid DESC",
                (project,),
            ):
                claim = json.loads(row[1])
                if claim["episode_id"] == episode_id:
                    trials.append(results.get(row[0], {**claim, "status": "claimed_unknown"}))
        if "creative_v2_trials" in tables:
            legacy = db.execute(
                "SELECT payload FROM creative_v2_trials WHERE project=? AND episode_id=?",
                (project, episode_id),
            ).fetchone()
            if legacy:
                trials.append({**json.loads(legacy[0]), "legacy_read_only": True})
        if candidate_versions:
            placeholders = ",".join("?" for _ in candidate_versions)
            observed = db.execute(
                "SELECT payload FROM creative_v2_runs WHERE project=? AND "
                f"json_extract(payload,'$.skill_version_id') IN ({placeholders}) "
                "ORDER BY rowid DESC LIMIT 20",
                (project, *candidate_versions),
            ).fetchall()
            existing_runs = {row.get("run_id") for row in trials if row.get("run_id")}
            for raw in observed:
                snapshot = json.loads(raw[0])
                version = candidate_versions[snapshot["skill_version_id"]]
                executed = snapshot.get("result") or {}
                if (
                    snapshot.get("skill_authorization", {}).get("selection") != "trial"
                    or (executed and executed.get("graph_hash") != version["execution_graph_hash"])
                    or (
                        not executed
                        and content_hash(snapshot["graph"]) != content_hash(version["graph"])
                    )
                    or snapshot["run_id"] in existing_runs
                ):
                    continue
                run = store.get_run(snapshot["run_id"])
                trials.append(
                    {
                        "trial_key": "observed-run:" + run.id,
                        "record_kind": "observed_trial_run",
                        "candidate_version_id": version["id"],
                        "run_id": run.id,
                        "status": run.status,
                        "error": run.error,
                        "source_record": {
                            "run_id": run.id,
                            "result_hash": content_hash(executed),
                            "skill_version_id": version["id"],
                            "version_hash": version["version_hash"],
                        },
                    }
                )
                existing_runs.add(run.id)
        for trial in trials:
            trial["execution_observation"] = _method_run_observation(
                db, project, trial.get("run_id")
            )
    detail["trials"] = trials
    return detail


def _method_run_observation(db, project, run_id):
    """Read-only provenance classification; absent mode is never assumed to be live."""
    unknown = {"mode": "unclassified", "confirmed_provider_responses": 0}
    if not run_id:
        return unknown
    stored = db.execute(
        "SELECT payload FROM creative_v2_runs WHERE run_id=? AND project=?", (run_id, project)
    ).fetchone()
    if stored is None:
        return unknown
    snapshot = json.loads(stored[0])
    requests = [
        json.loads(row[0])
        for row in db.execute(
            "SELECT request FROM creative_harness_dispatches WHERE run_id=?", (run_id,)
        )
    ]
    events = []
    for row in db.execute(
        "SELECT payload FROM creative_harness_events WHERE run_id=? ORDER BY seq", (run_id,)
    ):
        event = json.loads(row[0])
        if event.get("event_hash") != content_hash(
            {key: value for key, value in event.items() if key != "event_hash"}
        ):
            raise ValueError("方法运行轨迹完整性校验失败")
        events.append(event)
    mode = (snapshot.get("recompilation") or {}).get("execution_mode")
    if (
        not requests
        and mode == "stored_ir_recompilation_no_model_dispatch"
        and any(
            event["type"] == "recompilation/result"
            and event["payload"].get("execution_mode") == mode
            and event["payload"].get("model_dispatches") == 0
            for event in events
        )
    ):
        return {"mode": mode, "model_dispatches": 0, "confirmed_provider_responses": 0}
    allowed = {}
    for request in requests:
        if request["request_hash"] != content_hash(request["request"]):
            raise ValueError("方法模型请求完整性校验失败")
        config = request["request"].get("config", {})
        if "ChatModelProvider" in config.get("wrappers", []) and config.get("server_address"):
            allowed[request["id"]] = request
    confirmed = [
        event
        for event in events
        if event["type"] == "model/result"
        and event["payload"].get("request_id") in allowed
        and event["payload"].get("status") == "done"
        and event["payload"].get("measurement", {}).get("response_model")
        and event["payload"].get("measurement", {}).get("usage_status")
        == "provider_contract_reported"
    ]
    return {
        "mode": "live_generation" if confirmed else "unclassified",
        "model_dispatches": len(requests),
        "confirmed_provider_responses": len(confirmed),
    }


def prepare_auto_run(
    store,
    root,
    task,
    variant=None,
    *,
    prior_run_id=None,
    plan_guidance=None,
    machine_requirements=None,
    collaboration_policy=None,
    narrative_project_id=None,
    narrative_scene_id=None,
    narrative_preview_id=None,
    narrative_transition_id=None,
    provider=None,
    emit=None,
):
    from .v2_service import narrative_generation_context, prepare_v2_run

    if plan_guidance:
        raise ValueError("Edited plan cannot be silently replaced by automatic selection")
    requirements = _machine_values(machine_requirements)
    matches = NarrativeEvolutionStore(store).match(
        task.project,
        task.request.brief,
        resources=set(task.base_bundle.get("entities", {})),
        capabilities=CAPABILITIES,
        requirements=requirements,
        confirmed_requirement_keys=set(requirements),
        include_trials=False,
    )
    selected = next(
        (row for row in matches if row["eligible_to_execute"] and row["score"] > 0), None
    )
    if selected:
        return prepare_skill_run(
            store,
            root,
            task,
            selected["version_id"],
            prior_run_id=prior_run_id,
            machine_requirements=machine_requirements,
            collaboration_policy=collaboration_policy,
            narrative_project_id=narrative_project_id,
            narrative_scene_id=narrative_scene_id,
            narrative_preview_id=narrative_preview_id,
            narrative_transition_id=narrative_transition_id,
        )
    # Automatic mode cannot silently use variant/factory after an invalid model plan.
    planned = plan_initial_graph(
        store,
        root,
        task,
        requirements=machine_requirements,
        narrative_context=narrative_generation_context(
            store,
            task,
            narrative_project_id,
            narrative_scene_id,
            narrative_preview_id,
            narrative_transition_id,
            prior_run_id,
        ),
        provider=provider,
        emit=emit,
    )
    return prepare_v2_run(
        store,
        root,
        task,
        planned["graph"],
        prior_run_id=prior_run_id,
        machine_requirements=machine_requirements,
        collaboration_policy=collaboration_policy,
        narrative_project_id=narrative_project_id,
        narrative_scene_id=narrative_scene_id,
        narrative_preview_id=narrative_preview_id,
        narrative_transition_id=narrative_transition_id,
    )


def plan_initial_graph(
    store, root, task, *, requirements=None, narrative_context=None, provider=None, emit=None
):
    from .v2_planning import plan_initial_graph as run_planner

    return run_planner(
        store,
        root,
        task,
        requirements=requirements,
        narrative_context=narrative_context,
        provider=provider,
        emit=emit,
    )
