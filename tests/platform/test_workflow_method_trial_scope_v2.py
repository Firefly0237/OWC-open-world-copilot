"""Exact task trial scope uses production matching and revocable run admission."""

import pytest

from owcopilot.workflow import v2_service, v2_skills
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.method_trial_scope_v2 import (
    grant_trial_scope,
    prepare_trial_matched_run,
    revoke_trial_scope,
)
from tests.platform import test_workflow_v2_service as source_fixtures

studio = source_fixtures.studio


def test_scoped_trial_retrieval_is_task_bound_and_revocation_blocks_actual_admission(studio):
    root, store, task = studio
    skills = v2_skills.NarrativeEvolutionStore(store)
    family = skills.create_family("demo", "guard difficult choice")
    version = skills.add_version(
        "demo",
        family["id"],
        graph=create_narrative_graph().model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
    )
    skills.authorize_trial(
        "demo",
        version["id"],
        expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
        allowed=True,
        authority="preauthorized_policy",
        note="Controlled technical trial only",
    )
    scope = grant_trial_scope(
        store,
        "demo",
        task.id,
        [version["id"]],
        machine_requirements={"stage_count": 1},
        authority="development_technical_trial",
        reason="Controlled transfer admission fixture",
    )
    with pytest.raises(ValueError, match="不覆盖"):
        prepare_trial_matched_run(
            store,
            root,
            task,
            scope["id"],
            expected_permit_hash=scope["permit_hash"],
            machine_requirements={"stage_count": 2},
        )
    receipt = prepare_trial_matched_run(
        store,
        root,
        task,
        scope["id"],
        expected_permit_hash=scope["permit_hash"],
        machine_requirements={"stage_count": 1},
    )
    assert receipt["status"] == "prepared", receipt
    assert receipt["selection"]["selection"] == "authorized_trial"
    assert receipt["selection"]["retrieval"] == "sqlite_fts5_bm25_after_typed_admission"
    assert receipt["selection"]["version_id"] == version["id"]
    duplicate = prepare_trial_matched_run(
        store,
        root,
        task,
        scope["id"],
        expected_permit_hash=scope["permit_hash"],
        machine_requirements={"stage_count": 1},
    )
    assert duplicate == receipt
    run = v2_service.v2_detail(store, "demo", receipt["run_id"])
    v2_skills.check_run_authorization(store, "demo", run)
    revoke_trial_scope(
        store,
        "demo",
        scope["id"],
        expected_permit_hash=scope["permit_hash"],
        reason="Controlled revocation fixture",
    )
    with pytest.raises(ValueError, match="作用域已撤销"):
        v2_skills.check_run_authorization(store, "demo", run)
    current = skills.get_version("demo", version["id"])
    assert current["state"]["author_acceptance"] == "unknown"
    assert current["state"]["trial_status"] == "allowed"


def test_long_chinese_task_matches_shared_workflow_terms_without_exact_sentence(studio):
    _, store, task = studio
    skills = v2_skills.NarrativeEvolutionStore(store)
    family = skills.create_family("demo", "先验证决策合同再写对白")
    version = skills.add_version(
        "demo",
        family["id"],
        graph=create_narrative_graph().model_dump(mode="json"),
        expected_family_hash=family["family_hash"],
    )
    skills.authorize_trial(
        "demo",
        version["id"],
        expected_family_hash=skills.get_family("demo", family["id"])["family_hash"],
        allowed=True,
        authority="preauthorized_policy",
        note="Controlled query regression fixture",
    )
    matches = skills.match(
        "demo",
        "公开开发用例里玩家先整理账目争议的决策合同再生成合理对白并收束剧情",
        resources=set(task.base_bundle.get("entities", {})),
        capabilities=v2_skills.CAPABILITIES,
        requirements={},
        confirmed_requirement_keys=set(),
        include_trials=True,
    )
    assert matches[0]["version_id"] == version["id"] and matches[0]["score"] > 0
    assert matches[0]["retrieval"] == "sqlite_fts5_bm25_after_typed_admission"
    assert matches[0]["retrieval_query_mode"] == "cjk_bigram_disjunction"
