"""Real routing ledger dispatch with controlled transports, never human evidence."""

from __future__ import annotations

import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.budget_policy import BudgetUnknownError
from owcopilot.workflow.execution_records import ExecutionRecords
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.memory_routing import (
    assert_memory_route,
    memory_contract,
    route_task_memory,
)
from owcopilot.workflow.routed_memory import MemoryClusters
from owcopilot.workflow.skill_cards import SkillCards
from tests.platform.test_workflow_v2_service import studio as studio


class ControlledRouter:
    execution_origin = "controlled_transport"
    model = "controlled-memory-router"
    max_retries = 0

    def __init__(self, choose=None):
        self.calls = []
        self.choose = choose

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.calls.append(context)
        assert set(context) == {"instance", "memory_systems", "clusters", "output_schema"}
        for cluster in context["clusters"]:
            assert set(cluster) == {"id", "backend", "description", "completed_count"}
        answer = (
            self.choose(context)
            if self.choose
            else {
                "action": "create",
                "backend": "trajectory",
                "cluster_id": None,
                "description": "Short branching scenes with explicit state consequences",
                "reason": "Controlled protocol choice, not an efficacy judgment",
            }
        )
        return json.dumps(answer), 12, 4, 0, self.model


def test_existing_mode_has_no_router_dispatch_or_cluster_initialization(studio):
    root, store, task = studio
    provider = ControlledRouter()
    assert route_task_memory(store, root, task, provider=provider) is None
    assert provider.calls == []
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='creative_memory_clusters'"
            ).fetchone()
            is None
        )


def test_route_creates_exact_cluster_and_replays_frozen_receipt_without_new_call(studio):
    root, store, task = studio
    provider = ControlledRouter()
    receipt = route_task_memory(store, root, task, mode="routed", provider=provider)
    assert receipt["selection"] == "create"
    assert receipt["backend"] == "trajectory"
    assert receipt["context_max_tokens"] == 12000
    assert receipt["tokenizer_identity"] == receipt["contract"]["tokenizer_identity"]
    assert receipt["read_snapshot"]["payload"]["messages"] == []
    assert receipt["router_measurements"]["dispatch_count"] == 1
    assert receipt["router_measurements"]["total_tokens"] == 16
    decisions = ExecutionRecords(store).for_task("demo", task.id)
    assert decisions[0]["selected_action_id"] == "create:trajectory"
    assert decisions[0]["run_ids"] == [receipt["router_run_id"]]
    MemoryClusters(store).create(
        "demo", "trajectory", "Another unrelated group", contract=memory_contract(task)
    )
    replay = route_task_memory(store, root, task, mode="routed", provider=provider)
    assert replay == receipt
    assert len(provider.calls) == 1
    altered = {**receipt, "reason": "An unrecorded replacement"}
    altered["receipt_hash"] = content_hash(
        {key: value for key, value in altered.items() if key != "receipt_hash"}
    )
    with pytest.raises(ValueError, match="正式选择记录"):
        assert_memory_route(store, root, task, altered)


def test_instance_conditions_cannot_reroute_or_relabel_a_frozen_assignment(studio):
    root, store, task = studio
    provider = ControlledRouter()
    receipt = route_task_memory(
        store,
        root,
        task,
        mode="routed",
        requirements={"decision_option_count": 2},
        provider=provider,
    )
    with pytest.raises(ValueError, match="同一任务版本"):
        route_task_memory(
            store,
            root,
            task,
            mode="routed",
            requirements={"decision_option_count": 3},
            provider=provider,
        )
    with pytest.raises(ValueError, match="制作要求"):
        assert_memory_route(store, root, task, receipt, requirements={"decision_option_count": 3})
    with pytest.raises(ValueError, match="叙事上下文"):
        assert_memory_route(store, root, task, receipt, narrative_context={"scene": "changed"})
    assert_memory_route(
        store,
        root,
        task,
        receipt,
        requirements={"decision_option_count": 2},
        narrative_context=None,
        skill_bindings=[],
    )
    assert len(provider.calls) == 1


def test_inflight_instance_cannot_escape_by_changing_its_requirements(studio):
    root, store, task = studio
    duplicate = ControlledRouter()

    def during_call(context):
        with pytest.raises(ValueError, match="同一任务版本"):
            route_task_memory(
                store,
                root,
                task,
                mode="routed",
                requirements={"decision_option_count": 3},
                provider=duplicate,
            )
        return {
            "action": "create",
            "cluster_id": None,
            "backend": "trajectory",
            "description": "Controlled frozen instance",
            "reason": "Controlled protocol choice",
        }

    route_task_memory(
        store,
        root,
        task,
        mode="routed",
        requirements={"decision_option_count": 2},
        provider=ControlledRouter(during_call),
    )
    assert duplicate.calls == []


def test_reuse_sees_only_catalog_metadata_and_foreign_choice_never_falls_back(studio):
    root, store, task = studio
    clusters = MemoryClusters(store)
    allowed = clusters.create(
        "demo", "trajectory", "Shared short branches", contract=memory_contract(task)
    )
    foreign = clusters.create(
        "another-project", "trajectory", "Private project", contract=memory_contract(task)
    )

    def choose(context):
        assert [cluster["id"] for cluster in context["clusters"]] == [allowed["id"]]
        return {
            "action": "reuse",
            "cluster_id": foreign["id"],
            "backend": None,
            "description": None,
            "reason": "Invalid controlled counterexample",
        }

    provider = ControlledRouter(choose)
    with pytest.raises(ValueError, match="未提供或跨工作区"):
        route_task_memory(store, root, task, mode="routed", provider=provider)
    assert len(clusters.catalog("demo")) == 1
    with pytest.raises(ValueError, match="此前经历路由未完成"):
        route_task_memory(store, root, task, mode="routed", provider=provider)
    assert len(provider.calls) == 1
    decision = ExecutionRecords(store).for_task("demo", task.id)[0]
    assert decision["selected_action_id"] is None
    assert len(HarnessStore(store).requests(decision["run_ids"][0])) == 1


def test_unknown_transport_result_is_preserved_and_not_redispatched(studio):
    root, store, task = studio

    def timeout(context):
        raise TimeoutError("Controlled transport outcome unknown")

    provider = ControlledRouter(timeout)
    with pytest.raises(TimeoutError):
        route_task_memory(store, root, task, mode="routed", provider=provider)
    with pytest.raises(BudgetUnknownError, match="先对账"):
        route_task_memory(store, root, task, mode="routed", provider=provider)
    assert len(provider.calls) == 1
    decision = ExecutionRecords(store).for_task("demo", task.id)[0]
    assert decision["observation"]["status"] == "unknown"
    assert decision["observation"]["router_run_id"] in decision["run_ids"]


def test_concurrent_duplicate_cannot_claim_a_second_router_call(studio):
    root, store, task = studio
    duplicate = ControlledRouter()

    def during_call(context):
        with pytest.raises(BudgetUnknownError):
            route_task_memory(store, root, task, mode="routed", provider=duplicate)
        return {
            "action": "create",
            "backend": "trajectory",
            "cluster_id": None,
            "description": "A bounded group",
            "reason": "Single controlled winner",
        }

    provider = ControlledRouter(during_call)
    receipt = route_task_memory(store, root, task, mode="routed", provider=provider)
    assert receipt["router_measurements"]["dispatch_count"] == 1
    assert duplicate.calls == []


def test_author_selected_skill_is_not_adopted_and_withdrawal_blocks_old_route(studio):
    root, store, task = studio
    cards = SkillCards(store)
    card = cards.add_candidate(
        "demo",
        {
            "name": "Review consequences",
            "purpose": "Keep consequences explicit",
            "steps": [
                {
                    "id": "review",
                    "title": "Review effects",
                    "operation": "draft_quest",
                    "instruction": "State each option's consequence before expanding the scene.",
                }
            ],
            "applies_to": ["An explicit branching scene"],
            "does_not_apply_to": [],
        },
        {"kind": "import", "collection_condition": "controlled_protocol"},
    )[0]
    version = card["versions"][0]
    selected = {
        "card_id": card["id"],
        "version_id": version["id"],
        "version_hash": version["version_hash"],
    }
    receipt = route_task_memory(
        store,
        root,
        task,
        mode="routed",
        provider=ControlledRouter(),
        skill_card_selections=[selected],
        apply_project_skills=False,
    )
    assert receipt["skill_bindings"][0]["version_id"] == version["id"]
    assert cards.detail("demo", card["id"])["active_version_id"] is None
    current = cards.detail("demo", card["id"])
    cards.enable("demo", card["id"], current["card_hash"], False)
    with pytest.raises(ValueError, match="Skill选择"):
        assert_memory_route(store, root, task, receipt)


def test_reuse_keeps_selected_backend_and_does_not_create_another_cluster(studio):
    root, store, task = studio
    clusters = MemoryClusters(store)
    contract = memory_contract(task)
    clusters.create("demo", "trajectory", "Observed tool and production details", contract=contract)
    selected = clusters.create("demo", "strategy", "Reusable production lessons", contract=contract)

    def choose(context):
        assert len(context["clusters"]) == 2
        return {
            "action": "reuse",
            "cluster_id": selected["id"],
            "backend": None,
            "description": None,
            "reason": "Choose the method representation",
        }

    receipt = route_task_memory(store, root, task, mode="routed", provider=ControlledRouter(choose))
    assert receipt["selection"] == "reuse"
    assert receipt["cluster_id"] == selected["id"]
    assert receipt["backend"] == "strategy"
    assert receipt["read_snapshot"]["payload"]["lessons"] == []
    assert len(clusters.catalog("demo")) == 2
    decision = ExecutionRecords(store).for_task("demo", task.id)[0]
    assert decision["selected_action_id"] == "reuse:" + selected["id"]


def test_v3_exposes_current_contract_and_filters_legacy_skill_backend(studio):
    root, store, task = studio
    clusters = MemoryClusters(store)
    clusters.create("demo", "skill", "Legacy approved methods", contract=memory_contract(task))

    def choose(context):
        assert [row["id"] for row in context["memory_systems"]] == ["trajectory", "strategy"]
        assert context["clusters"] == []
        assert context["instance"]["entities"] == task.base_bundle.get("entities", {})
        assert context["instance"]["references"] == []
        assert context["instance"]["task_contract"] == task.request.model_dump(mode="json")
        assert "brief" in context["instance"]["task_schema"]["properties"]
        return {
            "action": "create",
            "cluster_id": None,
            "backend": "strategy",
            "description": "Independent procedural experiences",
            "reason": "Controlled v3",
        }

    receipt = route_task_memory(store, root, task, mode="routed", provider=ControlledRouter(choose))
    assert receipt["contract"]["adapter_protocol"] == "memory-routing-v3"
    assert receipt["read_snapshot"]["token_budget"]["max_tokens"] == 12000
    assert receipt["read_snapshot"]["payload"] == {"backend": "strategy", "lessons": []}
