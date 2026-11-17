"""Real repair driver, compiler, dispatch receipts and checks; controlled model data only."""

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow import repair_v2, v2_service
from owcopilot.workflow.collaboration_v2 import CollaborationStore
from owcopilot.workflow.execution_records import summarize_dispatches
from owcopilot.workflow.graph_delta import baseline_to_constrained_delta, project_legacy_graph_delta
from owcopilot.workflow.graph_v2 import GraphSpecV2, create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.skill_cards import SkillCards
from tests.platform.test_workflow_method_episode_v2 import VersionedNarrativeTransport
from tests.platform.test_workflow_v2_service import studio as studio


class MalformedLogic(VersionedNarrativeTransport):
    def complete(self, **kwargs):
        result = super().complete(**kwargs)
        if json.loads(kwargs["user"])["requirements"]["operation"] == "draft_logic":
            return json.dumps(
                {
                    "logic": {
                        "variables": [
                            {"id": "gold", "type": "int", "default": "not_an_integer"},
                        ]
                    },
                    "decisions": {},
                }
            ), *result[1:]
        return result


def broken_source(studio, provider=None, **prepare_options):
    root, store, task = studio
    graph = create_narrative_graph()
    source = v2_service.prepare_v2_run(
        store,
        root,
        task,
        graph.model_dump(mode="json"),
        collaboration_policy=prepare_options.pop(
            "collaboration_policy", {"allow_candidate_trial": True}
        ),
        **prepare_options,
    )
    result = v2_service.execute_v2_run(
        store,
        root,
        "demo",
        source["run_id"],
        provider=provider or MalformedLogic(),
    )
    assert result["result"]["outcomes"]["logic"]["status"] == "failed", result
    delta = baseline_to_constrained_delta(GraphSpecV2.model_validate(source["graph"]))
    edit = project_legacy_graph_delta(delta.model_dump(mode="json"))["edit"]
    return root, store, source, edit


def test_contract_failure_is_repaired_reexecuted_and_checked_with_old_budget(studio):
    root, store, source, edit = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    assert diagnosis["status"] == "localized" and diagnosis["failed_nodes"] == ["logic"]
    args = (store, root, "demo", source["run_id"], edit)
    options = {"expected_diagnosis_hash": diagnosis["diagnosis_hash"], "command_id": "repair-1"}
    prepared = repair_v2.prepare_repair_run(*args, **options)
    duplicate = repair_v2.prepare_repair_run(*args, **options)
    assert duplicate["prepared"]["run_id"] == prepared["prepared"]["run_id"]
    provider = VersionedNarrativeTransport()
    completed = repair_v2.execute_repair_run(
        store,
        root,
        "demo",
        prepared["repair_id"],
        provider=provider,
    )
    outcome = completed["outcome"]
    assert outcome["status"] == "completed" and outcome["Q0"] == "pass", completed
    assert outcome["Q1"] == outcome["Q2"] == "unknown"
    assert outcome["outcomes"]["plan"] == outcome["outcomes"]["quest"] == "reused"
    assert outcome["outcomes"]["logic"] == "succeeded"
    assert outcome["outcomes"]["audit"] == "succeeded" and outcome["checked_event_seqs"]
    requests = HarnessStore(store)
    old_logic = next(
        row for row in requests.requests(source["run_id"]) if row["node_id"] == "logic"
    )
    new_logic = next(
        row for row in requests.requests(outcome["run_id"]) if row["node_id"] == "logic"
    )
    assert old_logic["budget_id"] == new_logic["budget_id"]
    assert new_logic["attempt"] > old_logic["attempt"]
    count = len(provider.calls)
    assert (
        repair_v2.execute_repair_run(
            store,
            root,
            "demo",
            prepared["repair_id"],
            provider=provider,
        )["outcome"]
        == outcome
    )
    assert len(provider.calls) == count


def test_unknown_failure_refuses_locality_and_forged_contract_is_rejected(studio):
    class UnknownFailure(VersionedNarrativeTransport):
        def complete(self, **kwargs):
            if json.loads(kwargs["user"])["requirements"]["operation"] == "draft_logic":
                raise ValueError("An unclassified global inconsistency")
            return super().complete(**kwargs)

    root, store, source, edit = broken_source(studio, UnknownFailure())
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    assert diagnosis["status"] == "needs_evidence"
    assert diagnosis["unknown_action"] == "expand_or_reexplore"
    with pytest.raises(ValueError, match="未知语义"):
        repair_v2.prepare_repair_run(
            store,
            root,
            "demo",
            source["run_id"],
            edit,
            expected_diagnosis_hash=diagnosis["diagnosis_hash"],
            command_id="no-local-proof",
        )
    with pytest.raises(ValueError, match="完整性"):
        repair_v2.validate_repair_contract(
            store, "demo", {"repair_contract": {"invalidated_nodes": []}}
        )


def test_auto_guidance_repair_uses_real_errors_without_replacing_roles(studio):
    root, store, source, _edit = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    codes = {
        error["type"]
        for fault in diagnosis["faults"]
        for error in fault.get("validation_errors", [])
    }
    assert codes
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        source["run_id"],
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="automatic-schema-repair",
    )
    contract = prepared["prepared"]["contract"]
    child = v2_service.v2_detail(store, "demo", prepared["prepared"]["run_id"])
    assert child["graph"]["data_edges"] == source["graph"]["data_edges"]
    assert child["graph"]["control_edges"] == source["graph"]["control_edges"]
    assert [
        (row["id"], row["operation"], row["product_key"], row["problem_key"])
        for row in child["graph"]["nodes"]
    ] == [
        (row["id"], row["operation"], row["product_key"], row["problem_key"])
        for row in source["graph"]["nodes"]
    ]
    assert contract["graph_compilation"]["edit"]["operations"][0]["op"] == "update_guidance"

    class RepairAwareTransport(VersionedNarrativeTransport):
        def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["requirements"]["operation"] == "draft_logic":
                guidance = payload["guidance"]
                assert all(code in guidance for code in codes)
                assert "not_an_integer" not in guidance
                assert "保留原事实和保护项" in guidance
            return super().complete(**kwargs)

    provider = RepairAwareTransport()
    result = repair_v2.execute_repair_run(
        store,
        root,
        "demo",
        prepared["repair_id"],
        provider=provider,
    )
    assert result["outcome"]["status"] == "completed", result
    assert result["outcome"]["Q0"] == "pass"
    assert result["outcome"]["outcomes"]["plan"] == "reused"
    assert result["outcome"]["source_run_id"] == source["run_id"]
    assert result["outcome"]["method_candidate_scope"] == "source_task_only_until_parameterized"

    repaired = v2_service.v2_detail(store, "demo", result["outcome"]["run_id"])
    task = store.get_task(repaired["task_id"])
    full = v2_service.prepare_v2_run(
        store,
        root,
        task,
        repaired["graph"],
        machine_requirements=repaired["requirements"]["machine"],
        collaboration_policy=repaired["collaboration"]["policy"],
        apply_project_skills=False,
    )
    assert full.get("prior_run_id") is None
    assert full["graph_hash"] == repaired["graph_hash"]
    assert full["host_contract_identity"] == repaired["host_contract_identity"]
    assert full["requirements"]["contract_hash"] == repaired["requirements"]["contract_hash"]
    full = v2_service.execute_v2_run(store, root, "demo", full["run_id"], provider=provider)
    assert full["run"]["status"] == "completed"
    assert full["result"]["outcomes"]["audit"]["status"] == "succeeded"
    assert all(row["can_continue"] for row in full["requirements_report"].values())
    assert not any(
        row["severity"] == "error" for row in full["result"]["outputs"]["report"]["issues"]
    )
    source = v2_service.v2_detail(store, "demo", source["run_id"])

    def coverage(state, inherited=None):
        reports = []
        for node_id, outcome in state["result"]["outcomes"].items():
            origin = inherited if outcome["status"] == "reused" else state
            report = origin["requirements_report"][node_id]
            assert report["can_continue"]
            reports.append(
                {
                    "node_id": node_id,
                    "origin_run_id": origin["run_id"],
                    "report_hash": content_hash(report),
                    "contract_hash": report["contract_hash"],
                    "operation": report["operation"],
                    "semantic_status": report["semantic_status"],
                    "checks": [
                        {key: check[key] for key in ("key", "status", "blocking", "scope")}
                        for check in report["checks"]
                    ],
                }
            )
        return sorted(reports, key=lambda row: row["node_id"])

    repair_checks, full_checks = coverage(repaired, source), coverage(full)

    def comparable(reports):
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"origin_run_id", "report_hash"}
            }
            for row in reports
        ]

    assert comparable(repair_checks) == comparable(full_checks)

    def counted(state):
        requests = HarnessStore(store).requests(state["run_id"])
        measured = summarize_dispatches(requests, repair_v2._events(store, state["run_id"]))
        return {
            "run_id": state["run_id"],
            "status": state["run"]["status"],
            "graph_hash": state["graph_hash"],
            "result_hash": content_hash(state["result"]),
            "dispatch_count": measured["dispatch_count"],
            "calls": [
                {
                    "request_id": row["id"],
                    "node_id": row["node_id"],
                    "role_id": row["request"]["role_id"],
                    "budget_id": row["budget_id"],
                    "attempt": row["attempt"],
                    "request_hash": row["request_hash"],
                    "configuration_hash": row["configuration_hash"],
                }
                for row in requests
            ],
            "usage": {
                key: measured[key]
                for key in (
                    "known_totals",
                    "total_tokens",
                    "unknown_usage_request_ids",
                    "usage_complete",
                    "cost_usd",
                    "cost_status",
                )
            },
        }

    initial, local, rerun = counted(source), counted(repaired), counted(full)
    assert local["dispatch_count"] < rerun["dispatch_count"]
    for call in local["calls"]:
        assert any(
            (row["role_id"], row["configuration_hash"])
            == (call["role_id"], call["configuration_hash"])
            for row in rerun["calls"]
        )
    evidence = {
        "schema_version": 1,
        "evidence_origin": "controlled_test_transport",
        "comparison_scope": "one_reexecution_case_not_real_model_or_generalized_benefit",
        "database_path": str(store.path.resolve()),
        "task_id": task.id,
        "revision": task.revision,
        "task_input_hash": content_hash(task.request),
        "base_hash": task.base_hash,
        "protected_input_hash": content_hash(task.request.protected),
        "protected_item_count": len(task.request.protected),
        "author_intent_satisfaction": "unknown",
        "graph_identity_equal": repaired["graph_hash"] == full["graph_hash"],
        "execution_graph_identity_equal": (
            repaired["result"]["graph_hash"] == full["result"]["graph_hash"]
        ),
        "same_transport_instance": True,
        "formal_check_coverage_equal": True,
        "initial_failure_shared_by_both_arms": initial,
        "local_repair": {
            **local,
            "Q0": "pass",
            "Q1": "unknown",
            "Q2": "unknown",
            "effective_check_reports": repair_checks,
        },
        "full_rerun": {
            **rerun,
            "Q0": "pass",
            "Q1": "unknown",
            "Q2": "unknown",
            "effective_check_reports": full_checks,
        },
        "incremental_dispatch_difference": rerun["dispatch_count"] - local["dispatch_count"],
        "total_dispatches_including_shared_failure": {
            "local_repair": initial["dispatch_count"] + local["dispatch_count"],
            "full_rerun": initial["dispatch_count"] + rerun["dispatch_count"],
        },
        "limits": [
            "no human evaluation",
            "controlled token usage is not actual provider pricing",
            "cached nodes use source checks under unchanged input and graph reuse contracts",
            "no literary-quality or cross-task transfer claim",
        ],
    }
    evidence["evidence_hash"] = content_hash(evidence)
    (store.path.parent / "controlled-repair-comparison.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_prepared_repair_stops_if_source_trial_permission_is_revoked(studio):
    root, store, source, edit = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        source["run_id"],
        edit,
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="revoked-before-dispatch",
    )
    collaboration = CollaborationStore(store)
    with store.connection() as db:
        state = collaboration._read(db, "demo", source["run_id"])
        state["policy"]["allow_candidate_trial"] = False
        state["policy_hash"] = content_hash(state["policy"])
        state["policy_version"] += 1
        collaboration._write(db, source["run_id"], state)
    with pytest.raises(ValueError, match="许可已变化"):
        repair_v2.execute_repair_run(store, root, "demo", prepared["repair_id"], provider=object())
    assert HarnessStore(store).requests(prepared["prepared"]["run_id"]) == []


def test_known_host_failure_is_reconciled_without_a_second_dispatch(studio):
    root, store, source, edit = broken_source(studio)
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        source["run_id"],
        edit,
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="host-failed",
    )
    run_id = prepared["prepared"]["run_id"]
    repair_v2._append(store, "demo", prepared["repair_id"], "execution_claim", {"run_id": run_id})
    store.update_run(run_id, status="failed", error="Controlled persisted host failure")
    reconciled = repair_v2.execute_repair_run(
        store,
        root,
        "demo",
        prepared["repair_id"],
        provider=object(),
    )
    assert reconciled["outcome"]["status"] == "failed"
    assert reconciled["outcome"]["Q0"] == "unknown"
    assert reconciled["outcome"]["actual_dispatch_ids"] == []


def test_automatic_repair_keeps_the_source_task_explicit_skill_selection(studio):
    _root, store, _task = studio
    cards = SkillCards(store)
    card, _created = cards.add_candidate(
        "demo",
        {
            "name": "Preserve mechanics",
            "purpose": "Keep declared mechanics when drafting logic",
            "steps": [
                {
                    "id": "keep",
                    "title": "Keep mechanics",
                    "operation": "draft_logic",
                    "instruction": "Preserve the current declared mechanics when drafting logic.",
                }
            ],
            "applies_to": ["Logic drafts with declared mechanics"],
        },
        {"kind": "import"},
    )
    version = card["versions"][0]
    selection = {
        "card_id": card["id"],
        "version_id": version["id"],
        "version_hash": version["version_hash"],
    }
    root, store, source, _edit = broken_source(
        studio,
        skill_card_selections=[selection],
        apply_project_skills=False,
    )
    diagnosis = repair_v2.diagnose_repair(store, "demo", source["run_id"])
    prepared = repair_v2.prepare_repair_run(
        store,
        root,
        "demo",
        source["run_id"],
        expected_diagnosis_hash=diagnosis["diagnosis_hash"],
        command_id="skill-preserving-repair",
    )
    child = v2_service.v2_detail(store, "demo", prepared["prepared"]["run_id"])
    assert child["skill_card_bindings"] == source["skill_card_bindings"]
    assert child["graph_hash"] == prepared["prepared"]["contract"]["target_graph_hash"]
    node = next(row for row in child["graph"]["nodes"] if row["id"] == "logic")
    assert "Preserve the current declared mechanics" in node["guidance"]
    assert "validation_error_types" in node["guidance"]
    result = repair_v2.execute_repair_run(
        store,
        root,
        "demo",
        prepared["repair_id"],
        provider=VersionedNarrativeTransport(),
    )
    assert result["outcome"]["status"] == "completed" and result["outcome"]["Q0"] == "pass"
    assert cards.detail("demo", card["id"])["active_version_id"] is None


@pytest.mark.parametrize("authorized,provider_fault", [(True, False), (False, False), (True, True)])
def test_production_auto_repair_uses_new_guidance_once_and_respects_default_denial(
    studio,
    monkeypatch,
    authorized,
    provider_fault,
):
    class CorrectedByGuidance(MalformedLogic):
        def complete(self, **kwargs):
            payload = json.loads(kwargs["user"])
            if payload["requirements"]["operation"] == "draft_logic":
                if provider_fault:
                    GraphSpecV2.model_validate({})
                if "validation_error_types" in payload["guidance"]:
                    return VersionedNarrativeTransport.complete(self, **kwargs)
            return super().complete(**kwargs)

    provider = CorrectedByGuidance()
    root, store, source, _edit = broken_source(
        studio,
        provider,
        summarize_skill=True,
        collaboration_policy={"allow_candidate_trial": True} if authorized else {},
    )
    source = v2_service.v2_detail(store, "demo", source["run_id"])
    if provider_fault:
        assert repair_v2.diagnose_repair(store, "demo", source["run_id"])["status"] == "localized"
    before = len(provider.calls)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_kwargs: provider)
    repaired = repair_v2.maybe_repair_execution(store, root, "demo", source)
    if not authorized or provider_fault:
        assert repaired is source and len(provider.calls) == before
        return
    assert repaired["run"]["status"] == "completed"
    assert repaired["summarize_skill"] is True
    assert repaired["automatic_repair"]["source_run_id"] == source["run_id"]
    assert repaired["result"]["outcomes"]["plan"]["status"] == "reused"
    assert repaired["result"]["outcomes"]["quest"]["status"] == "reused"
    assert repaired["result"]["outcomes"]["logic"]["status"] == "succeeded"
    count = len(provider.calls)
    assert count > before
    duplicate = repair_v2.maybe_repair_execution(store, root, "demo", source)
    assert duplicate["run_id"] == repaired["run_id"] and len(provider.calls) == count
    assert repair_v2.maybe_repair_execution(store, root, "demo", repaired) is repaired
    assert len(provider.calls) == count


def test_source_cancellation_after_prepare_stops_child_with_inherited_job(studio, monkeypatch):
    root, store, source, _edit = broken_source(studio)
    store.update_run(source["run_id"], job_id="controlled-production-job")
    source = v2_service.v2_detail(store, "demo", source["run_id"])
    saved_result = content_hash(source["result"])
    execute = repair_v2.execute_repair_run
    child_ids = []

    def cancel_before_execution(store, root, project, repair_id, **kwargs):
        record = repair_v2.get_repair(store, project, repair_id)
        child_id = record["prepared"]["run_id"]
        child_ids.append(child_id)
        assert store.get_run(child_id).job_id == "controlled-production-job"
        store.update_run(source["run_id"], cancel_requested=True)
        return execute(store, root, project, repair_id, **kwargs)

    def no_provider(**_kwargs):
        raise AssertionError("Cancellation must stop before building a provider")

    monkeypatch.setattr(repair_v2, "execute_repair_run", cancel_before_execution)
    monkeypatch.setattr(v2_service, "build_real_provider", no_provider)
    assert repair_v2.maybe_repair_execution(store, root, "demo", source) is source
    assert len(child_ids) == 1
    child = v2_service.v2_detail(store, "demo", child_ids[0])
    assert HarnessStore(store).requests(child_ids[0]) == []
    with pytest.raises(ValueError, match="源运行已请求取消"):
        repair_v2.validate_repair_contract(store, "demo", child)
    current_source = v2_service.v2_detail(store, "demo", source["run_id"])
    assert content_hash(current_source["result"]) == saved_result
    assert repair_v2.maybe_repair_execution(store, root, "demo", source) is source
    assert len(child_ids) == 1
