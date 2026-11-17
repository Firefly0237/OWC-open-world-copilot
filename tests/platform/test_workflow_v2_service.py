"""Integrated V2 composition with explicitly controlled development transports.

No test calls a real model or supplies evidence of real author satisfaction. The
production graph driver, Harness, persistence, validators and previews stay real.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, DialogueTree, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.gateway import ToolCall, ToolCallResponse
from owcopilot.service.api import create_app
from owcopilot.workflow import v2_method_runtime, v2_service, v2_skills
from owcopilot.workflow.graph_delta import (
    baseline_to_constrained_delta,
    project_legacy_graph_delta,
)
from owcopilot.workflow.graph_v2 import GraphSpecV2, create_narrative_graph, rewrite_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.mechanic_ir_v2 import (
    DialogueDesignIRV3,
    MechanicDesignIRV2,
    QuestDraftV2,
)
from owcopilot.workflow.method_trial_scope_v2 import grant_trial_scope
from owcopilot.workflow.models import EventPlan, TaskInput
from owcopilot.workflow.store import RunInterruptedError

PREFIX = "/projects/demo/creative/v2"


def _narrative_case():
    plan = EventPlan.model_validate(
        {
            "contract_version": 1,
            "premise": "A guard requests help, with two different state consequences",
            "beats": ["Learn the guard's motive", "Choose how to help"],
            "events": [{"id": "gate", "summary": "Decide how to help"}],
            "decisions": [
                {
                    "id": "help",
                    "event_id": "gate",
                    "options": ["Pay", "Refuse"],
                    "consequences": ["Trust rises", "Trust falls"],
                }
            ],
        }
    )
    quest = Quest.model_validate(
        {
            "id": "q",
            "title": "Guard at the gate",
            "objective": "Choose whether to help",
            "stages": [{"id": "gate", "summary": "Talk"}],
            "logic": {
                "variables": [
                    {
                        "id": "route",
                        "type": "enum",
                        "enum_values": ["unset", "pay", "refuse"],
                        "default": "unset",
                    },
                    {"id": "trust", "type": "int", "default": 0},
                ],
                "branches": [
                    {
                        "id": "pay",
                        "from_stage": "gate",
                        "condition": "route == 'pay'",
                        "outcome": "paid",
                        "effects": [{"var": "trust", "op": "inc", "value": 1}],
                    },
                    {
                        "id": "refuse",
                        "from_stage": "gate",
                        "condition": "route == 'refuse'",
                        "outcome": "refused",
                        "effects": [{"var": "trust", "op": "dec", "value": 1}],
                    },
                ],
            },
        }
    )
    decisions = {
        "quest_id": "q",
        "options": [
            {
                "decision_id": "help",
                "option_index": index,
                "branch_id": branch,
                "effect_indices": [0],
            }
            for index, branch in enumerate(("pay", "refuse"))
        ],
    }
    tree = DialogueTree.model_validate(
        {
            "id": "tree",
            "quest_id": "q",
            "root_node": "gate",
            "nodes": {
                "gate": {
                    "id": "gate",
                    "text": "Will you help?",
                    "choices": [
                        {"text": "Pay", "effects": [{"var": "route", "value": "pay"}]},
                        {"text": "Refuse", "effects": [{"var": "route", "value": "refuse"}]},
                    ],
                }
            },
        }
    )
    choices = {
        "quest_id": "q",
        "tree_id": "tree",
        "choices": [
            {
                "decision_id": "help",
                "option_index": index,
                "node_id": "gate",
                "choice_index": index,
            }
            for index in range(2)
        ],
    }
    return plan, quest, decisions, tree, choices


class ControlledNarrativeProvider:
    """Pydantic-valid transport inputs; never a substitute for live model evidence."""

    model = "controlled-development-narrative"
    max_retries = 0

    def __init__(self, *, fail_logic=False):
        self.calls = []
        self.fail_logic = fail_logic

    def complete(self, *, system, user, model):
        payload = json.loads(user)
        self.calls.append({"system": system, "user": payload, "model": model})
        plan, quest, decisions, tree, choices = _narrative_case()
        operation = payload["requirements"]["operation"]
        if operation == "plan_events":
            value = plan.model_dump(mode="json")
        elif operation == "draft_quest":
            assert payload["output_schema"]["title"] == "QuestDraftV2"
            value = QuestDraftV2(
                title=quest.title,
                objective=quest.objective,
                stages=[stage.model_dump(mode="json") for stage in quest.stages],
            ).model_dump(mode="json")
        elif operation == "draft_logic":
            if self.fail_logic:
                raise ConnectionError("Controlled development network failure")
            decisions.update(
                schema_version=2,
                quest_id=payload["inputs"]["quest"]["id"],
                selector_variables=["route"],
            )
            value = {"logic": quest.logic.model_dump(mode="json"), "decisions": decisions}
        elif operation == "logic_contract":
            if self.fail_logic:
                raise ConnectionError("Controlled development network failure")
            assert payload["output_schema"]["title"] == "MechanicDesignIRV2"
            assert payload["inputs"]["quest"]["id"].startswith("q_")
            value = MechanicDesignIRV2(
                purpose="Paying and refusing have different trust consequences",
                variables=[{"id": "trust", "type": "int", "default": 0}],
                stages=[{
                    "stage_id": "gate",
                    "purpose": "Resolve whether to help",
                    "decision_id": "help",
                    "options": [{
                        "purpose": option,
                        "outcome": outcome,
                        "effects": [{"var": "trust", "op": "inc", "value": change}],
                    } for option, outcome, change in (
                        ("Pay", "paid", 1), ("Refuse", "refused", -1),
                    )],
                }],
            ).model_dump(mode="json")
        elif operation == "dialogue_constrained":
            assert payload["output_schema"]["title"] == "DialogueDesignIRV3"
            assert payload["inputs"]["contract"]["status"] == "validated"
            assert payload["inputs"]["contract"]["mapping"]["schema_version"] == 2
            value = DialogueDesignIRV3(stages=[{
                "stage_id": "gate",
                "text": "Will you help?",
                "options": [
                    {
                        "text": "Pay", "response_text": "You hand over the payment.",
                        "ending_text": "The guard takes the payment and opens the gate.",
                    },
                    {
                        "text": "Refuse", "response_text": "You keep your coins.",
                        "ending_text": "The guard turns away and closes the gate.",
                    },
                ],
            }]).model_dump(mode="json")
        elif operation in {"draft_dialogue", "bind_dialogue"}:
            value = tree.model_dump(mode="json")
            if "condition为空，effects为空" in system:
                value = deepcopy(value)
                for choice in value["nodes"]["gate"]["choices"]:
                    choice["effects"] = []
        else:
            raise AssertionError("Unexpected production model output contract: " + operation)
        return json.dumps(value), 21, 11, 0, self.model


class ControlledNativeProposal:
    """Exercises the real native-tool protocol with a single development delta."""

    model = "controlled-development-native"
    max_retries = 0
    supports_tools = True

    def __init__(self):
        self.calls = []
        self.curator_calls = []
        self.context = None
        self.source_graph = None

    @staticmethod
    def _entry(context):
        return {
            "id": "distinct-consequences",
            "operation": "logic_contract",
            "parts": [{
                "kind": "text",
                "text": "Keep selector effects separate from distinct consequences",
            }],
            "evidence_kind": "hypothesis",
            "support_refs": context["allowed_source_refs"],
        }

    def complete_with_tools(self, *, messages, tools, model):
        self.calls.append(deepcopy(messages))
        if len(self.calls) == 1:
            self.context = json.loads(messages[-1]["content"])
            assert tools[0]["function"]["name"] == "read_feedback_evidence"
            evidence_id = tools[0]["function"]["parameters"]["properties"]["evidence_id"]["enum"][0]
            return ToolCallResponse(
                tool_calls=[
                    ToolCall(
                        "read-one",
                        "read_feedback_evidence",
                        {"evidence_id": evidence_id},
                    )
                ]
            )
        assert len(self.calls) == 2
        outcome = json.loads(messages[-1]["content"])
        assert outcome["is_error"] is False
        snapshot = json.loads(outcome["value"]["evidence_json"])
        assert content_hash(snapshot) == outcome["value"]["snapshot_hash"]
        assert "distinct consequences" in snapshot["feedback"]
        self.source_graph = GraphSpecV2.model_validate(snapshot["graph"])
        return ToolCallResponse(
            text=json.dumps(
                {
                    "protocol": "method-reflection-v3",
                    "problem_id": self.context["problem_id"],
                    "target_description": "Check distinct consequences before writing dialogue",
                    "diagnosis": "Dialogue currently precedes an explicit decision-contract gate",
                    "evidence_refs": self.context["allowed_source_refs"],
                    "reusable_entries": [self._entry(self.context)],
                    "proposed_change": "mixed",
                }
            )
        )

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.curator_calls.append(context)
        assert "Curator" in system and len(self.curator_calls) == 1
        assert context["problem_id"] == self.context["problem_id"]
        delta = baseline_to_constrained_delta(self.source_graph).model_dump(mode="json")
        edit = project_legacy_graph_delta(delta)["edit"]
        assert edit["expected_graph_hash"] == context["expected_graph_hash"]
        return json.dumps({
            "protocol": "method-proposal-v3",
            "problem_id": context["problem_id"],
            "target_description": "Check distinct consequences before writing dialogue",
            "rationale": "Controlled business edit grounded in the observed source graph",
            "change_kind": "mixed",
            "graph_edit": edit,
            "upsert_entries": [self._entry(context)],
        }), 9, 7, 0, self.model


@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON", json.dumps({"demo": str(root), "alias": str(root)})
    )
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_RUNTIME_DIR", raising=False)
    store = open_workflow_store(root)
    task = create_task_action(
        root,
        "demo",
        TaskInput(brief="Controlled development input: a guard's difficult choice"),
    )["task"]
    return root, store, store.get_task(task["id"])


def _execute(root, store, task, variant="constrained", *, provider=None, **kwargs):
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph(variant).model_dump(mode="json"),
        **kwargs,
    )
    provider = provider or ControlledNarrativeProvider()
    state = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    return state, provider


def test_production_graph_harness_artifact_and_preview_are_available_under_scoped_http(studio):
    root, store, task = studio
    state, provider = _execute(
        root, store, task, plan_guidance={"logic": "Keep the guard's stated motive"}
    )
    assert state["run"]["status"] == "completed", state
    assert state["result"]["outputs"]["report"]["coverage"]["mechanical_status"] == "validated"
    assert state["result"]["outputs"]["report"]["semantic_status"] == "withheld"
    source = state["feedback_source"]
    artifact = store.get_artifact(source["artifact_id"])
    assert artifact.content_hash == content_hash(artifact.payload) == source["artifact_hash"]
    assert (store.path.parent / "graph-v2-checkpoints.sqlite3").is_file()
    requests = HarnessStore(store).requests(state["run_id"])
    assert len(requests) == len(provider.calls) == 4
    logic_request = next(row for row in requests if row["node_id"] == "logic")
    assert "Keep the guard's stated motive" in logic_request["request"]["user"]
    assert logic_request["context_manifest"]["task"]["base_hash"] == task.base_hash

    with TestClient(create_app()) as client:
        run_id = state["run_id"]
        assert client.get(f"{PREFIX}/runs/{run_id}").json()["run_id"] == run_id
        detail = client.get(f"{PREFIX}/tasks/{task.id}").json()
        assert detail["runs"][0]["run_id"] == run_id
        first = client.get(f"{PREFIX}/runs/{run_id}/events?limit=1").json()
        second = client.get(
            f"{PREFIX}/runs/{run_id}/events?after_seq={first['after_seq']}&limit=1"
        ).json()
        assert second["events"][0]["seq"] > first["events"][0]["seq"]
        assert client.get(f"{PREFIX}/runs/{run_id}/requests").json()["requests"] == requests
        for suffix in ("", "/events", "/requests", "/evidence"):
            response = client.get(f"/projects/alias/creative/v2/runs/{run_id}{suffix}")
            assert response.status_code == 404, response.text
        assert client.post(f"{PREFIX}/tasks/{task.id}/runs", json={}).status_code == 503
        assert client.post(f"{PREFIX}/tasks/{task.id}/runs", json={"graph": {}}).status_code == 422
        preview_response = client.post(f"{PREFIX}/runs/{run_id}/preview", json={})
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        bound = state["result"]["outputs"]["bound"]
        binding = bound["tree"]["metadata"]["player_presentation_v1"]["bindings"][0]
        choice_id = f"{binding['node_id']}:{binding['choice_index']}"
        assert preview["state"]["trust"] == 0
        assert [choice["label"] for choice in preview["choices"]] == ["Pay", "Refuse"]
        assert preview["choices"][0]["id"] == choice_id
        selected = client.post(
            f"{PREFIX}/previews/{preview['preview_id']}/step",
            json={"action_id": "choose-pay", "choice_id": choice_id, "expected_seq": 0},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["state"] == {binding["selector_variable"]: binding["option_value"],
                                           "trust": 0}
        assert selected.json()["text"] == "You hand over the payment."
        assert selected.json()["choices"] == []
        finished = client.post(
            f"{PREFIX}/previews/{preview['preview_id']}/step",
            json={"action_id": "apply-pay", "expected_seq": 1},
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["state"]["trust"] == 1
        assert finished.json()["status"] == "completed"
        assert finished.json()["text"] == "The guard takes the payment and opens the gate."
        settled = [step for step in finished.json()["history"] if step["phase"] == "quest"]
        assert len(settled) == 1 and settled[0]["choice_id"] == binding["branch_id"]
    assert len(provider.calls) == 4  # Observing and previewing never calls the model.
    assert ContentStore(root).load().quests == {}  # Only immutable drafts were produced.


def test_baseline_native_evolution_trial_and_partial_reuse_consume_changed_graph(studio):
    root, store, task = studio
    baseline, _ = _execute(root, store, task, "baseline")
    assert baseline["run"]["status"] == "completed", baseline
    saved = v2_service.save_run_skill(store, "demo", baseline["run_id"], name="Guard method")
    skills = v2_service.evolution_store(store)
    family_id = saved["family"]["id"]
    parent_id = saved["version"]["id"]
    assert saved["version"]["state"]["author_acceptance"] == "unknown"
    assert saved["family"]["default_version_id"] is None
    native = ControlledNativeProposal()
    episode = v2_skills.evolve_family(
        store,
        root,
        "demo",
        family_id,
        baseline["run_id"],
        "Controlled development feedback: ensure distinct consequences before writing dialogue",
        provider=native,
    )
    assert episode["result"]["status"] == "candidate", episode
    assert len(native.calls) == 2 and len(native.curator_calls) == 1
    candidate_id = episode["result"]["candidate_version_id"]
    candidate = skills.get_version("demo", candidate_id)
    assert candidate["parent_version_id"] == parent_id
    assert candidate["state"]["technical_status"] == "compiled"
    assert candidate["state"]["author_acceptance"] == "unknown"
    assert skills.get_family("demo", family_id)["default_version_id"] is None
    assert len(skills.versions("demo", family_id)) == 2
    tool_events = HarnessStore(store).events(episode["trace_run_id"])
    tool_result = next(row for row in tool_events if row["type"] == "tool/result")
    assert tool_result["payload"]["is_error"] is False
    dispatches = HarnessStore(store).requests(episode["trace_run_id"])
    assert [row["attempt"] for row in dispatches] == [1, 2, 3]
    assert len({row["budget_id"] for row in dispatches}) == 1
    repeated = v2_skills.evolve_family(
        store,
        root,
        "demo",
        family_id,
        baseline["run_id"],
        "Controlled development feedback: ensure distinct consequences before writing dialogue",
        provider=native,
    )
    assert repeated["id"] == episode["id"] and repeated["result"] == episode["result"]
    assert len(native.calls) == 2 and len(native.curator_calls) == 1
    assert len(skills.versions("demo", family_id)) == 2

    with pytest.raises(ValueError, match="许可"):
        v2_service.prepare_skill_run(store, root, task, candidate_id)
    skills.authorize_trial(
        "demo",
        candidate_id,
        expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
        allowed=True,
        authority="explicit_author_action",
        note="Controlled development trial permission; not real author acceptance",
    )
    recalled = skills.match(
        "demo",
        "distinct consequences",
        resources=set(),
        capabilities=v2_skills.CAPABILITIES,
        include_trials=True,
    )
    assert recalled[0]["version_id"] == candidate_id
    assert recalled[0]["retrieval"] == "sqlite_fts5_bm25_after_typed_admission"
    machine = baseline["requirements"]["machine"]
    scope = grant_trial_scope(
        store, "demo", task.id, [candidate_id], machine_requirements=machine,
        authority="development_technical_trial", reason="Controlled candidate execution",
    )
    prepared = v2_service.prepare_skill_run(
        store, root, task, candidate_id, prior_run_id=baseline["run_id"],
        machine_requirements=machine,
        trial_scope_id=scope["id"], expected_trial_scope_hash=scope["permit_hash"],
    )
    provider = ControlledNarrativeProvider()
    result = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert result["run"]["status"] == "completed", result
    outcomes = result["result"]["outcomes"]
    assert outcomes["plan"]["status"] == outcomes["quest"]["status"] == "reused"
    assert outcomes["logic"]["operation"] == "logic_contract"
    assert outcomes["dialogue"]["operation"] == "dialogue_constrained"
    assert outcomes["logic"]["status"] == outcomes["dialogue"]["status"] == "succeeded"
    assert len(provider.calls) == 2
    requests = HarnessStore(store).requests(prepared["run_id"])
    logic_request = next(row for row in requests if row["node_id"] == "logic")
    assert (
        "Keep selector effects separate from distinct consequences"
        in (logic_request["request"]["user"])
    )
    assert candidate["execution_graph_hash"] == result["result"]["graph_hash"]
    assert skills.get_version("demo", candidate_id)["state"]["author_acceptance"] == "unknown"
    assert skills.get_family("demo", family_id)["default_version_id"] is None
    assert ContentStore(root).load().quests == {}


def test_provider_failure_keeps_diagnostics_without_creative_candidate_or_hidden_retry(studio):
    root, store, task = studio
    state, provider = _execute(
        root, store, task, provider=ControlledNarrativeProvider(fail_logic=True)
    )
    assert state["run"]["status"] == "completed_with_skips", state
    assert state["feedback_source"] is None
    assert state["result"]["outcomes"]["logic"]["status"] == "failed"
    assert len(provider.calls) == 3  # plan, quest, one failed logic; no automatic retries.
    saved = v2_service.save_run_skill(store, "demo", state["run_id"], name="Failure diagnosis")
    skills = v2_service.evolution_store(store)
    assert saved["version"]["state"]["technical_status"] == "failed"
    native = ControlledNativeProposal()
    result = v2_skills.evolve_family(
        store,
        root,
        "demo",
        saved["family"]["id"],
        state["run_id"],
        "Controlled development observation: network disconnected",
        provider=native,
    )
    assert result["status"] == "operational_failure_recorded"
    assert result["candidate"] is None and native.calls == []
    assert len(skills.versions("demo", saved["family"]["id"])) == 1
    assert skills.get_family("demo", saved["family"]["id"])["default_version_id"] is None


@pytest.mark.parametrize(
    "boundary",
    [
        "trial_revoked",
        "graph_tampered",
        "native_initialization",
    ],
)
def test_v2_execution_boundaries_fail_before_dispatch_and_do_not_orphan_tasks(
    studio, monkeypatch, boundary
):
    """Targeted final check for the three independently identified composition defects."""
    root, store, task = studio
    provider = ControlledNarrativeProvider()
    if boundary == "graph_tampered":
        prepared = v2_service.prepare_v2_run(
            store, root, task, create_narrative_graph("constrained").model_dump(mode="json")
        )
        tampered = deepcopy(prepared)
        tampered["graph"]["nodes"][0]["guidance"] = "Unrecorded changed guidance"
        with store.connection() as db:
            db.execute(
                "UPDATE creative_v2_runs SET payload=? WHERE run_id=?",
                (json.dumps(tampered), prepared["run_id"]),
            )
        with pytest.raises(ValueError, match="hash"):
            v2_service.v2_detail(store, "demo", prepared["run_id"])
        with pytest.raises(ValueError, match="hash"):
            v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
        assert provider.calls == []
        return

    baseline, _ = _execute(root, store, task, "baseline")
    assert baseline["run"]["status"] == "completed", baseline
    saved = v2_service.save_run_skill(
        store, "demo", baseline["run_id"], name="Controlled boundary fixture"
    )
    skills = v2_service.evolution_store(store)
    family_id = saved["family"]["id"]
    version_id = saved["version"]["id"]
    if boundary == "trial_revoked":
        # A separate candidate makes revocation version-specific without adopting the baseline.
        parent_graph = GraphSpecV2.model_validate(saved["version"]["graph"])
        logic = next(node for node in parent_graph.nodes if node.id == "logic")
        candidate_graph = rewrite_graph(
            parent_graph,
            nodes_upsert=[logic.model_copy(update={"guidance": "Keep the guard's goal explicit"})],
        )
        candidate = skills.add_version(
            "demo",
            family_id,
            graph=candidate_graph.model_dump(mode="json"),
            parent_version_id=version_id,
            expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
        )
        assert candidate["execution_graph_hash"] != saved["version"]["execution_graph_hash"]
        version_id = candidate["id"]
        skills.authorize_trial(
            "demo",
            version_id,
            expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
            allowed=True,
            authority="explicit_author_action",
            note="Controlled development trial authorization",
        )
        machine = baseline["requirements"]["machine"]
        scope = grant_trial_scope(
            store, "demo", task.id, [version_id], machine_requirements=machine,
            authority="development_technical_trial", reason="Controlled revocation check",
        )
        prepared = v2_service.prepare_skill_run(
            store, root, task, version_id, machine_requirements=machine,
            trial_scope_id=scope["id"], expected_trial_scope_hash=scope["permit_hash"],
        )
        skills.authorize_trial(
            "demo",
            version_id,
            expected_family_hash=skills.get_family("demo", family_id)["family_hash"],
            allowed=False,
            authority="explicit_author_action",
            note="Controlled development revocation before the queued worker starts",
        )
        try:
            state = v2_service.execute_v2_run(
                store, root, "demo", prepared["run_id"], provider=provider
            )
        except (ValueError, RunInterruptedError):
            pass  # An entry gate may reject before an execution result exists.
        else:
            assert state["run"]["status"] in {"cancelled", "failed", "interrupted"}
            assert state["feedback_source"] is None
        assert provider.calls == []
        assert HarnessStore(store).requests(prepared["run_id"]) == []
        return

    def fail_initialization(**kwargs):
        raise RuntimeError("Controlled development native initialization failure")

    monkeypatch.setattr(v2_method_runtime, "build_real_provider", fail_initialization)
    try:
        v2_skills.evolve_family(
            store,
            root,
            "demo",
            family_id,
            baseline["run_id"],
            "Controlled development feedback: distinct consequences are required",
        )
    except RuntimeError as exc:
        assert "native initialization failure" in str(exc)
    assert not any(row.status in {"queued", "running"} for row in store.list_runs(task.id))
    prepared = v2_service.prepare_v2_run(
        store, root, task, create_narrative_graph("constrained").model_dump(mode="json")
    )
    assert prepared["task_id"] == task.id  # No orphan trace blocks subsequent independent work.
    assert provider.calls == []
