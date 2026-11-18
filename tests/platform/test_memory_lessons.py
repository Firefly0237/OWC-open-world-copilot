"""Actual experience formation and retrieval with explicitly controlled transports."""

import json
from copy import deepcopy

import pytest

from owcopilot.app.actions.workflow import create_task_action
from owcopilot.content.hash import content_hash
from owcopilot.workflow import memory_lessons
from owcopilot.workflow.budget_policy import BudgetUnknownError
from owcopilot.workflow.execution_records import ExecutionRecords
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.memory_lessons import (
    form_experience_lessons,
    rebuild_experience_lessons,
    select_experience_lessons,
)
from owcopilot.workflow.memory_routing import route_task_memory
from owcopilot.workflow.models import TaskInput
from owcopilot.workflow.routed_memory import MemoryClusters, read_memory_source
from tests.platform.test_memory_routing import ControlledRouter
from tests.platform.test_workflow_v2_service import _execute
from tests.platform.test_workflow_v2_service import studio as studio


class ControlledLessonEncoder:
    identity = {
        "model": "controlled-semantic-protocol",
        "revision": "a" * 40,
        "normalized": True,
        "local_files_only": True,
    }

    def __init__(self):
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        return [1.0, 0.0]


class ControlledLessonProvider:
    execution_origin = "controlled_transport"
    max_retries = 0
    model = "controlled-experience-protocol"

    def __init__(self, *, empty=False, unknown=False, wrong_source=False):
        self.calls = []
        self.empty, self.unknown, self.wrong_source = empty, unknown, wrong_source

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.calls.append((context, model))
        assert context["visible_messages"]
        assert not any(key in context for key in ("reward", "gold", "human_rating"))
        if "allowed_source_ref" not in context:
            value = {"outcome": "success", "rationale": "Controlled visible technical completion"}
        else:
            if self.unknown:
                raise TimeoutError("Controlled extraction acknowledgement unknown")
            source_ref = dict(context["allowed_source_ref"])
            if self.wrong_source:
                source_ref["id"] = "unprovided-source"
            value = {
                "items": []
                if self.empty
                else [
                    {
                        "title": "Check state consequences",
                        "description": "Independent branching scenes with distinct consequences",
                        "content": "Map decisions to state effects before expanding prose.",
                        "source_ref": source_ref,
                    }
                ]
            }
        return json.dumps(value), 13, 7, 0, self.model


@pytest.fixture
def strategy_case(studio, monkeypatch):
    root, store, task = studio
    monkeypatch.setattr(
        memory_lessons, "lesson_encoder_identity", lambda: ControlledLessonEncoder.identity
    )
    router = ControlledRouter(
        lambda context: {
            "action": "create",
            "cluster_id": None,
            "backend": "strategy",
            "description": "Independent branching scenes with reusable state checks",
            "reason": "Controlled strategy lifecycle",
        }
    )
    route = route_task_memory(store, root, task, mode="routed", provider=router)
    state, _ = _execute(root, store, task, memory_route=route)
    assert state["run"]["status"] == "completed", state
    with store.connection() as db:
        _, source = read_memory_source(db, "demo", state["run_id"], source_schema_version=3)
    assert source["skill_bindings"] == []
    assert source["visible_messages"]
    return root, store, task, state, source


def test_selected_cluster_forms_new_lessons_without_old_skills_and_reuses_exact_receipt(
    strategy_case, monkeypatch
):
    root, store, _, state, source = strategy_case
    provider, encoder = ControlledLessonProvider(), ControlledLessonEncoder()
    route = state["memory_route"]
    clusters = MemoryClusters(store)
    updated = clusters.append_run(
        "demo",
        route["cluster_id"],
        state["run_id"],
        root=root,
        expected_state_hash=route["state_hash"],
        lesson_provider=provider,
        encoder=encoder,
    )
    assert updated["learning_status"] == "completed"
    assert len(provider.calls) == 2
    assert len(encoder.calls) == 1
    with store.connection() as db:
        member = json.loads(
            db.execute(
                "SELECT payload FROM creative_memory_members WHERE run_id=?", (state["run_id"],)
            ).fetchone()[0]
        )
    formation = member["lesson_formation"]
    assert formation["source_hash"] == content_hash(source)
    assert formation["model_proxy"]["assessment_kind"] == "model_proxy"
    assert formation["model_proxy"]["author_acceptance"] == "unknown"
    assert formation["generalization_status"] == "not_verified"
    assert formation["measurements"]["dispatch_count"] == 2
    requests = HarnessStore(store).requests(formation["formation_run_id"])
    assert [request["request"]["role_id"] for request in requests] == [
        "experience_outcome_proxy",
        "experience_lesson_extract",
    ]
    assert all(request["request"]["model"] == provider.model for request in requests)
    assert formation["model"] == provider.model
    assert formation["execution_origin"] == "controlled_transport"
    assert formation["source_model_contract"] == route["contract"]["model"]
    decision = ExecutionRecords(store).for_task("demo", state["task_id"])[0]
    assert formation["formation_run_id"] in decision["run_ids"]
    replay = form_experience_lessons(
        store,
        root,
        "demo",
        route["cluster_id"],
        state["run_id"],
        source,
        provider=provider,
        encoder=encoder,
    )
    assert replay == formation
    assert len(provider.calls) == 2
    next_task = store.get_task(
        create_task_action(root, "demo", TaskInput(brief="Another independent branching scene"))[
            "task"
        ]["id"]
    )
    monkeypatch.setattr(memory_lessons, "get_lesson_encoder", lambda: encoder)
    snapshot = clusters.read_snapshot("demo", route["cluster_id"], root=root, task=next_task)
    assert snapshot["payload"]["lessons"][0]["source_run_id"] == state["run_id"]
    assert snapshot["payload"]["lessons"][0]["formation_hash"] == formation["formation_hash"]
    assert snapshot["retrieval"]["selection"] == "cosine_nearest_request"
    calls = len(encoder.calls)
    clusters.assert_snapshot("demo", snapshot, root=root, task=next_task)
    assert len(encoder.calls) == calls


def test_empty_extraction_is_recorded_without_inventing_new_advice(strategy_case):
    root, store, _, state, source = strategy_case
    provider = ControlledLessonProvider(empty=True)
    formation = form_experience_lessons(
        store,
        root,
        "demo",
        state["memory_route"]["cluster_id"],
        state["run_id"],
        source,
        provider=provider,
        encoder=ControlledLessonEncoder(),
    )
    assert formation["status"] == "no_change"
    assert formation["items"] == []
    assert formation["measurements"]["dispatch_count"] == 2


@pytest.mark.parametrize("failure", ["unknown", "wrong_source"])
def test_failed_or_unknown_formation_does_not_replay_models(strategy_case, failure):
    root, store, _, state, source = strategy_case
    provider = ControlledLessonProvider(**{failure: True})
    encoder = ControlledLessonEncoder()
    args = (store, root, "demo", state["memory_route"]["cluster_id"], state["run_id"], source)
    with pytest.raises(BudgetUnknownError if failure == "unknown" else ValueError):
        form_experience_lessons(*args, provider=provider, encoder=encoder)
    with pytest.raises(BudgetUnknownError if failure == "unknown" else ValueError):
        form_experience_lessons(*args, provider=provider, encoder=encoder)
    assert len(provider.calls) == 2
    assert store.get_run(state["run_id"]).status == "completed"


class CharacterCounter:
    identity = {"kind": "controlled_character_test_counter"}

    def count(self, text):
        return len(text)


def _member(run_id, vector):
    source = {"run_id": run_id, "observations": "Minimal controlled source"}
    source_hash = content_hash(source)
    source_ref = {"kind": "run", "id": run_id, "content_hash": source_hash}
    formation = {
        "source_run_id": run_id,
        "source_hash": source_hash,
        "cluster_id": "controlled-cluster",
        "formation_run_id": "formation-" + run_id,
        "items": [
            {
                "title": "First",
                "description": "A relevant case",
                "content": "Do the check.",
                "source_ref": source_ref,
            },
            {
                "title": "Second",
                "description": "A second check",
                "content": "Keep evidence.",
                "source_ref": source_ref,
            },
        ],
        "request_embedding": {
            "identity": ControlledLessonEncoder.identity,
            "vector": vector,
            "vector_hash": content_hash(vector),
        },
    }
    formation["formation_hash"] = content_hash(formation)
    return (
        {
            "run_id": run_id,
            "source_hash": source_hash,
            "cluster_id": "controlled-cluster",
            "lesson_formation": formation,
        },
        {},
        source,
    )


def test_cosine_selects_nearest_earlier_experience_and_keeps_the_entire_group(studio, monkeypatch):
    _, _, task = studio
    monkeypatch.setattr(
        memory_lessons, "lesson_encoder_identity", lambda: ControlledLessonEncoder.identity
    )
    members = [_member("earlier-nearest", [1.0, 0.0]), _member("latest-unrelated", [0.0, 1.0])]
    counter, encoder = CharacterCounter(), ControlledLessonEncoder()
    payload, retrieval = select_experience_lessons(members, task, {}, counter, 10000, encoder)
    assert retrieval["selected_source_run_id"] == "earlier-nearest"
    assert len(payload["lessons"]) == 2
    assert all(row["source_run_id"] == "earlier-nearest" for row in payload["lessons"])
    assert rebuild_experience_lessons(members, task, {}, retrieval, counter, 10000) == payload
    assert len(encoder.calls) == 1
    changed = deepcopy(retrieval)
    changed["selected_source_run_id"] = "latest-unrelated"
    with pytest.raises(ValueError, match="冻结经历检索"):
        rebuild_experience_lessons(members, task, {}, changed, counter, 10000)
    empty, limited = select_experience_lessons(members, task, {}, counter, 20, encoder)
    assert empty["lessons"] == []
    assert limited["status"] == "selected_group_exceeds_budget"


def test_vector_space_mismatch_is_rejected_without_hash_or_recency_fallback(studio, monkeypatch):
    _, _, task = studio
    monkeypatch.setattr(
        memory_lessons, "lesson_encoder_identity", lambda: ControlledLessonEncoder.identity
    )
    member, state, source = _member("other-space", [1.0, 0.0])
    formation = member["lesson_formation"]
    formation["request_embedding"]["identity"] = {"model": "other-space", "revision": "b" * 40}
    formation["formation_hash"] = content_hash(
        {key: value for key, value in formation.items() if key != "formation_hash"}
    )
    with pytest.raises(ValueError, match="语义空间"):
        select_experience_lessons(
            [(member, state, source)],
            task,
            {},
            CharacterCounter(),
            10000,
            ControlledLessonEncoder(),
        )


def test_real_schema_failure_forms_a_source_bound_lesson_for_the_next_task(studio, monkeypatch):
    from owcopilot.workflow import memory_history, routed_memory, v2_service
    from owcopilot.workflow.graph_v2 import create_narrative_graph
    from owcopilot.workflow.skill_cards import SkillCards

    class FailureLessonTransport(ControlledLessonProvider):
        model = "controlled-failure-to-lesson"

        def complete(self, *, system, user, model):
            context = json.loads(user)
            self.calls.append((context, model))
            if "visible_messages" not in context:
                assert context["requirements"]["operation"] == "plan_events"
                value = {
                    "contract_version": 1,
                    "premise": "An explicitly controlled failed attempt",
                    "beats": "This must be a list, not a string",
                    "events": [],
                    "decisions": [],
                }
            else:
                feedback = [
                    json.loads(message["content"])
                    for message in context["visible_messages"]
                    if message.get("feedback_ref", {}).get("type") == "graph.node.failed"
                ]
                assert feedback and feedback[0]["observation"]["status"] == "failed"
                assert not any(key in context for key in ("reward", "gold", "human_rating"))
                if "allowed_source_ref" not in context:
                    value = {
                        "outcome": "failure",
                        "rationale": "The real typed check rejected beats and blocked later nodes.",
                    }
                else:
                    assert context["model_proxy"]["outcome"] == "failure"
                    value = {
                        "items": [
                            {
                                "title": "Validate list-shaped plan fields",
                                "description": (
                                    "Use for typed planning outputs; "
                                    "not a judgment of story quality."
                                ),
                                "content": (
                                    "Read the current output schema "
                                    "and emit arrays for list fields "
                                    "before expanding downstream content."
                                ),
                                "source_ref": context["allowed_source_ref"],
                            }
                        ]
                    }
            return json.dumps(value), 13, 7, 0, self.model

    root, store, _ = studio
    provider, encoder, counter = (
        FailureLessonTransport(),
        ControlledLessonEncoder(),
        CharacterCounter(),
    )
    monkeypatch.setattr(memory_history, "get_memory_token_counter", lambda: counter)
    monkeypatch.setattr(routed_memory, "get_memory_token_counter", lambda: counter)
    monkeypatch.setattr(memory_lessons, "get_lesson_encoder", lambda: encoder)
    monkeypatch.setattr(
        memory_lessons, "lesson_encoder_identity", lambda: ControlledLessonEncoder.identity
    )
    cards = SkillCards(store)
    skills_before = deepcopy(cards.list("demo"))
    task = store.get_task(
        create_task_action(
            root,
            "demo",
            TaskInput(
                brief="Produce a controlled branching scene through typed planning",
                model=provider.model,
            ),
        )["task"]["id"]
    )
    router = ControlledRouter(
        lambda context: {
            "action": "create",
            "cluster_id": None,
            "backend": "strategy",
            "description": "Typed planning attempts and source-bound failure avoidance",
            "reason": "Controlled failure-to-lesson lifecycle",
        }
    )
    router.model = provider.model
    route = route_task_memory(
        store, root, task, mode="routed", provider=router, token_counter=counter
    )
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        memory_route=route,
    )
    graph_before, graph_hash = deepcopy(prepared["graph"]), prepared["graph_hash"]
    ended = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert ended["run"]["status"] == "completed_with_skips"
    assert ended["result"]["outcomes"]["plan"]["status"] == "failed"
    assert any(
        value["status"] == "blocked_dependency" for value in ended["result"]["outcomes"].values()
    )
    assert ended["graph"] == graph_before and ended["graph_hash"] == graph_hash
    with store.connection() as db:
        _, source = read_memory_source(db, "demo", ended["run_id"])
    assert source["current_observation_classification"] == "observed_failure"
    assert source["has_production_outputs"] is False
    assert source["skill_bindings"] == []
    clusters = MemoryClusters(store)
    update = clusters.append_run(
        "demo",
        route["cluster_id"],
        ended["run_id"],
        root=root,
        expected_state_hash=route["state_hash"],
        lesson_provider=provider,
        encoder=encoder,
        counter=counter,
    )
    assert update["status"] == "appended"
    assert update["observation_kind"] == "observed_failure"
    assert update["learning_status"] == "completed" and update["lesson_count"] == 1
    with store.connection() as db:
        member = json.loads(
            db.execute(
                "SELECT payload FROM creative_memory_members WHERE run_id=?", (ended["run_id"],)
            ).fetchone()[0]
        )
    formation = member["lesson_formation"]
    assert formation["model_proxy"]["outcome"] == "failure"
    assert formation["model_proxy"]["assessment_kind"] == "model_proxy"
    assert formation["model_proxy"]["author_acceptance"] == "unknown"
    assert formation["model_proxy"]["literary_quality"] == "unknown"
    assert formation["generalization_status"] == "not_verified"
    assert formation["source_hash"] == content_hash(source)
    assert formation["items"][0]["source_ref"] == {
        "kind": "run",
        "id": ended["run_id"],
        "content_hash": content_hash(source),
    }
    trace = HarnessStore(store)
    production_requests = trace.requests(ended["run_id"])
    formation_requests = trace.requests(formation["formation_run_id"])
    assert len(production_requests) == 1 and len(formation_requests) == 2
    assert {
        request["request"]["model"] for request in [*production_requests, *formation_requests]
    } == {provider.model}
    assert formation["model"] == formation["source_model_contract"] == provider.model
    for request in formation_requests:
        actual = json.loads(request["request"]["user"])
        assert actual["visible_messages"] == source["visible_messages"]
        assert any(
            message.get("feedback_ref", {}).get("type") == "graph.node.failed"
            for message in actual["visible_messages"]
        )
    next_task = store.get_task(
        create_task_action(
            root,
            "demo",
            TaskInput(
                brief="Plan another independent typed branching scene",
                model=provider.model,
            ),
        )["task"]["id"]
    )
    snapshot = clusters.read_snapshot(
        "demo", route["cluster_id"], root=root, task=next_task, counter=counter
    )
    assert snapshot["retrieval"]["selected_source_run_id"] == ended["run_id"]
    assert snapshot["payload"]["lessons"][0]["source_ref"] == formation["items"][0]["source_ref"]
    assert snapshot["payload"]["lessons"][0]["content"] == formation["items"][0]["content"]
    clusters.assert_snapshot("demo", snapshot, root=root, task=next_task, counter=counter)
    assert cards.list("demo") == skills_before
    persisted = v2_service.v2_detail(store, "demo", ended["run_id"])
    assert persisted["graph"] == graph_before and persisted["graph_hash"] == graph_hash
