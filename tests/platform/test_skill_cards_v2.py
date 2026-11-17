"""Actual production dispatch/binding with controlled transports; no live or human claims."""

from __future__ import annotations

import json
import time
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from owcopilot.app.actions.workflow import create_task_action
from owcopilot.service.api import create_app
from owcopilot.workflow import skill_learning, v2_service
from owcopilot.workflow.budget_policy import BudgetLedger
from owcopilot.workflow.execution_records import summarize_dispatches
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.models import TaskCreateInput
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.skill_learning import learn_skill
from tests.platform.test_workflow_v2_service import ControlledNarrativeProvider, _execute
from tests.platform.test_workflow_v2_service import studio as studio

PREFIX = "/projects/demo/creative/v2"


class ControlledSkillSummary:
    model = "controlled-protocol-summary"
    max_retries = 0
    execution_origin = "controlled_transport"

    def __init__(self):
        self.calls = []

    def complete(self, *, system, user, model):
        context = json.loads(user)
        self.calls.append(context)
        assert context["observed_steps"]
        assert any(row["status"] == "succeeded" for row in context["observed_steps"])
        assert any(
            row["binding_key"] == "decision_option_count"
            for row in context["available_parameter_bindings"]
        )
        value = {
            "should_save": True,
            "reason": "Controlled protocol candidate, not efficacy evidence",
            "name": "Plan explicit alternatives",
            "purpose": "Respect the current choice count",
            "steps": [
                {
                    "title": "Plan current alternatives",
                    "method": {
                        "id": "alternatives",
                        "operation": "draft_quest",
                        "evidence_kind": "hypothesis",
                        "parts": [
                            {"kind": "text", "text": "Use "},
                            {
                                "kind": "binding",
                                "binding_kind": "requirement",
                                "binding_key": "decision_option_count",
                            },
                            {"kind": "text", "text": " alternatives for this task."},
                        ],
                        "support_refs": context["allowed_source_refs"],
                        "counterexample_refs": [],
                    },
                }
            ],
            "applies_to": ["A scene with a declared choice count"],
            "does_not_apply_to": ["Changing locked content"],
            "changes": [],
        }
        return json.dumps(value), 17, 9, 0, self.model


class ControlledThreeChoiceProvider(ControlledNarrativeProvider):
    """Three fixed valid alternatives, used only to check current-parameter dispatch."""

    execution_origin = "controlled_transport"

    def complete(self, *, system, user, model):
        result = super().complete(system=system, user=user, model=model)
        context = json.loads(user)
        value = json.loads(result[0])
        if context["requirements"]["machine"].get("decision_option_count") == 3:
            operation = context["requirements"]["operation"]
            if operation == "plan_events":
                value["decisions"][0]["options"].append("Negotiate")
                value["decisions"][0]["consequences"].append("A compromise is reached")
            elif operation == "logic_contract":
                value["stages"][0]["options"].append(
                    {
                        "purpose": "Negotiate",
                        "outcome": "A compromise is reached",
                        "effects": [{"var": "trust", "op": "inc", "value": 2}],
                    }
                )
            elif operation == "dialogue_constrained":
                value["stages"][0]["options"].append(
                    {
                        "text": "Negotiate",
                        "response_text": "You propose a compromise.",
                        "ending_text": "The guard agrees to a compromise.",
                    }
                )
        return json.dumps(value), *result[1:]


def _selection(card):
    version = next(row for row in card["versions"] if row["id"] == card["current_version_id"])
    return {
        "card_id": card["id"],
        "version_id": version["id"],
        "version_hash": version["version_hash"],
    }


def _new_task(root, store, brief):
    created = create_task_action(
        root,
        "demo",
        TaskCreateInput(
            brief=brief,
            origin_kind="development",
        ),
    )
    return store.get_task(created["task"]["id"])


def test_source_summary_candidate_then_explicit_new_task_dispatch_rebinds_current_count(studio):
    root, store, task = studio
    source_provider = ControlledThreeChoiceProvider()
    source, _ = _execute(
        root,
        store,
        task,
        provider=source_provider,
        machine_requirements={"decision_option_count": 2},
    )
    assert source["run"]["status"] == "completed"
    summary = ControlledSkillSummary()
    learned = learn_skill(
        store, root, "demo", source["run_id"], operation_id="first-summary", provider=summary
    )
    assert learned["status"] == "candidate"
    cards = SkillCards(store)
    card = cards.detail("demo", learned["card_id"])
    assert card["active_version_id"] is None
    assert cards.resolve("demo") == []
    assert card["author_quality"] == "unknown"
    assert card["versions"][0]["source"]["author_acceptance"] == "unknown"
    # A different operation label must not redispatch the same frozen source summary.
    learn_skill(
        store,
        root,
        "demo",
        source["run_id"],
        operation_id="same-source-new-label",
        provider=summary,
    )
    assert len(summary.calls) == 1
    assert len(cards.detail("demo", card["id"])["versions"]) == 1

    target = _new_task(root, store, "Controlled new scene: offer a third route to negotiate")
    provider = ControlledThreeChoiceProvider()
    selected, _ = _execute(
        root,
        store,
        target,
        provider=provider,
        machine_requirements={"decision_option_count": 3},
        skill_card_selections=[_selection(card)],
        apply_project_skills=False,
    )
    assert selected["run"]["status"] == "completed"
    actual = next(
        call
        for call in provider.calls
        if call["user"]["requirements"]["operation"] == "draft_quest"
    )
    assert "Use 3 alternatives for this task." in actual["user"]["guidance"]
    assert "Use 2 alternatives for this task." not in actual["user"]["guidance"]
    assert cards.detail("demo", card["id"])["active_version_id"] is None
    requests = HarnessStore(store).requests(selected["run_id"])
    quest_request = next(row for row in requests if row["request"]["role_id"] == "draft_quest")
    assert quest_request["request"]["config"]["execution_origin"] == "controlled_transport"
    assert (
        quest_request["context_manifest"]["skill_cards"][0]["version_id"] == learned["version_id"]
    )
    saved_user = json.loads(quest_request["request"]["user"])
    assert saved_user["guidance"] == actual["user"]["guidance"]
    with store.connection() as db:
        consumed = [
            json.loads(row[0])["payload"]
            for row in db.execute(
                "SELECT payload FROM skill_card_events WHERE card_id=? "
                "AND json_extract(payload,'$.kind')='consumed' "
                "AND json_extract(payload,'$.payload.run_id')=?",
                (card["id"], selected["run_id"]),
            )
        ]
    assert {row["node_id"] for row in consumed} == {quest_request["node_id"]}
    assert cards.detail("demo", card["id"])["author_quality"] == "unknown"


def test_revocation_during_response_prevents_late_content_publication(studio):
    root, store, task = studio
    cards = SkillCards(store)
    card, _ = cards.add_candidate(
        "demo",
        {
            "name": "Keep a motive open",
            "purpose": "Leave room for later explanation",
            "steps": [
                {
                    "id": "motive",
                    "title": "Consider motive",
                    "operation": "plan_events",
                    "instruction": "Avoid explaining away every motive.",
                }
            ],
            "applies_to": ["An investigative scene"],
            "does_not_apply_to": [],
        },
        {
            "kind": "run",
            "run_id": "synthetic-protocol-source",
            "execution_origin": "controlled_transport",
            "author_acceptance": "unknown",
        },
    )
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph("constrained").model_dump(mode="json"),
        skill_card_selections=[_selection(card)],
        apply_project_skills=False,
    )

    class RevokeOnResponse(ControlledNarrativeProvider):
        execution_origin = "controlled_transport"

        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            current = cards.detail("demo", card["id"])
            cards.enable("demo", card["id"], current["card_hash"], False)
            return result

    provider = RevokeOnResponse()
    result = v2_service.execute_v2_run(store, root, "demo", prepared["run_id"], provider=provider)
    assert len(provider.calls) == 1
    assert result["run"]["status"] == "failed"
    assert not [row for row in store.artifacts(task.id) if row.run_id == prepared["run_id"]]
    events = HarnessStore(store).events(prepared["run_id"], limit=1000)
    assert not any(
        event["type"] == "model/result" and "response" in event["payload"] for event in events
    )
    assert cards.detail("demo", card["id"])["author_quality"] == "unknown"


def _finished_http_job(client, response):
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        observed = client.get(f"/jobs/{job_id}")
        assert observed.status_code == 200, observed.text
        state = observed.json()
        if state["status"] in {"done", "failed"}:
            assert state["status"] == "done", state
            assert any(event["type"] == "done" for event in state["events"])
            return state["result"]
        time.sleep(0.25)
    raise AssertionError(f"Controlled HTTP job did not finish: {job_id}")


def test_http_package_manifest_zip_and_import_keep_candidate_scope(studio):
    _, store, _ = studio
    card, _ = SkillCards(store).add_candidate(
        "demo",
        {
            "name": "Keep consequences readable",
            "purpose": "Separate choice from state effects",
            "steps": [
                {
                    "id": "effects",
                    "title": "Check effects",
                    "operation": "draft_quest",
                    "instruction": "State the consequence before expanding the dialogue.",
                }
            ],
            "applies_to": ["A scene with a meaningful choice"],
            "does_not_apply_to": [],
        },
        {
            "kind": "import",
            "collection_condition": "synthetic_protocol",
            "execution_origin": "no_model",
            "author_quality": "unknown",
        },
    )
    version_id = card["current_version_id"]
    with TestClient(create_app()) as client:
        listed = client.get(PREFIX + "/skill-cards")
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()["cards"]] == [card["id"]]
        endpoint = f"{PREFIX}/skill-cards/{card['id']}/versions/{version_id}"
        manifest = client.get(endpoint + "/package-manifest")
        archive = client.get(endpoint + "/package")
        assert manifest.status_code == archive.status_code == 200
        assert archive.headers["content-type"] == "application/zip"
        assert ".zip" in archive.headers["content-disposition"]
        files = manifest.json()["files"]
        with ZipFile(BytesIO(archive.content)) as package:
            assert {name: package.read(name).decode("utf-8") for name in package.namelist()} == {
                row["path"]: row["content"] for row in files
            }
        assert any(row["path"].endswith("/SKILL.md") for row in files)
        imported = client.post(
            "/projects/alias/creative/v2/skill-cards/import", json={"files": files}
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["active_version_id"] is None
        assert imported.json()["author_quality"] == "unknown"
        assert imported.json()["versions"][0]["source"]["kind"] == "import"
        observed = client.get("/projects/alias/creative/v2/skill-cards")
        assert len(observed.json()["cards"]) == 1
        changed = [dict(row) for row in files]
        changed[0]["content"] += "\nUnrecorded edit"
        rejected = client.post(
            "/projects/alias/creative/v2/skill-cards/import", json={"files": changed}
        )
        assert rejected.status_code == 409, rejected.text


def test_http_summary_selection_update_and_default_disable_cas(studio, monkeypatch):
    root, store, task = studio
    source, _ = _execute(
        root,
        store,
        task,
        provider=ControlledThreeChoiceProvider(),
        machine_requirements={"decision_option_count": 2},
    )
    assert source["run"]["status"] == "completed"

    class UpdatingSummary(ControlledSkillSummary):
        def complete(self, **kwargs):
            raw = super().complete(**kwargs)
            value = json.loads(raw[0])
            if self.calls[-1]["parent"] is not None:
                value["steps"][0]["method"]["parts"][-1]["text"] += " Review the state effect."
                value["changes"] = ["Add a current-state review instruction"]
            return json.dumps(value), *raw[1:]

    summary = UpdatingSummary()
    production = ControlledThreeChoiceProvider()
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-test-key-not-a-provider-credential")
    monkeypatch.setattr(skill_learning, "build_real_provider", lambda **_: summary)
    monkeypatch.setattr(v2_service, "build_real_provider", lambda **_: production)
    with TestClient(create_app()) as client:
        learned = _finished_http_job(
            client,
            client.post(
                PREFIX + "/skill-cards/from-run",
                json={
                    "run_id": source["run_id"],
                    "operation_id": "http-summary",
                },
            ),
        )
        assert learned["status"] == "candidate"
        cards = client.get(PREFIX + "/skill-cards").json()["cards"]
        card = next(row for row in cards if row["id"] == learned["card_id"])
        assert card["active_version_id"] is None
        created = client.post(
            PREFIX + "/tasks",
            json={
                "brief": "Controlled next scene: negotiate as the third alternative",
                "origin_kind": "development",
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]
        executed = _finished_http_job(
            client,
            client.post(
                f"{PREFIX}/tasks/{task_id}/production-jobs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 3},
                    "skill_card_selections": [_selection(card)],
                    "apply_project_skills": False,
                    "summarize_skill": False,
                },
            ),
        )
        assert executed["run"]["status"] == "completed", executed
        supplied = client.get(f"{PREFIX}/runs/{executed['run_id']}/requests")
        assert supplied.status_code == 200, supplied.text
        quest = next(
            row for row in supplied.json()["requests"] if row["request"]["role_id"] == "draft_quest"
        )
        assert (
            "Use 3 alternatives for this task." in json.loads(quest["request"]["user"])["guidance"]
        )
        assert quest["context_manifest"]["skill_cards"][0]["version_id"] == learned["version_id"]
        before_adoption = card["card_hash"]
        adopted = client.post(
            f"{PREFIX}/skill-cards/{card['id']}/adopt",
            json={
                "expected_card_hash": before_adoption,
                "version_id": learned["version_id"],
                "make_default": True,
            },
        )
        assert adopted.status_code == 200, adopted.text
        adopted = adopted.json()
        stale = client.post(
            f"{PREFIX}/skill-cards/{card['id']}/enable",
            json={
                "expected_card_hash": before_adoption,
                "enabled": False,
            },
        )
        assert stale.status_code == 409, stale.text
        updated = _finished_http_job(
            client,
            client.post(
                f"{PREFIX}/skill-cards/{card['id']}/update",
                json={
                    "run_id": executed["run_id"],
                    "operation_id": "http-update",
                    "expected_card_hash": adopted["card_hash"],
                    "base_version_id": learned["version_id"],
                    "feedback": "Review the current state effect before drafting dialogue.",
                },
            ),
        )
        assert updated["status"] == "candidate"
        assert updated["version_id"] != learned["version_id"]
        current = next(
            row
            for row in client.get(PREFIX + "/skill-cards").json()["cards"]
            if row["id"] == card["id"]
        )
        assert len(current["versions"]) == 2
        assert current["active_version_id"] == learned["version_id"]
        assert "Review the state effect." not in json.loads(quest["request"]["user"])["guidance"]
        next_task = client.post(
            PREFIX + "/tasks",
            json={
                "brief": "Controlled next scene: negotiate as the third alternative",
                "origin_kind": "development",
            },
        )
        assert next_task.status_code == 201, next_task.text
        next_task_id = next_task.json()["task"]["id"]
        updated_execution = _finished_http_job(
            client,
            client.post(
                f"{PREFIX}/tasks/{next_task_id}/production-jobs",
                json={
                    "selection": "template",
                    "variant": "constrained",
                    "machine_requirements": {"decision_option_count": 3},
                    "skill_card_selections": [_selection(current)],
                    "apply_project_skills": True,
                    "summarize_skill": False,
                },
            ),
        )
        assert updated_execution["run"]["status"] == "completed", updated_execution
        actual_update = client.get(f"{PREFIX}/runs/{updated_execution['run_id']}/requests")
        assert actual_update.status_code == 200, actual_update.text
        updated_quest = next(
            row
            for row in actual_update.json()["requests"]
            if row["request"]["role_id"] == "draft_quest"
        )
        updated_guidance = json.loads(updated_quest["request"]["user"])["guidance"]
        assert "Use 3 alternatives for this task." in updated_guidance
        assert "Review the state effect." in updated_guidance
        assert (
            updated_quest["context_manifest"]["skill_cards"][0]["version_id"]
            == updated["version_id"]
        )
        current = next(
            row
            for row in client.get(PREFIX + "/skill-cards").json()["cards"]
            if row["id"] == card["id"]
        )
        assert current["active_version_id"] == learned["version_id"]
        disabled = client.post(
            f"{PREFIX}/skill-cards/{card['id']}/enable",
            json={
                "expected_card_hash": current["card_hash"],
                "enabled": False,
            },
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False
        assert disabled.json()["author_quality"] == "unknown"
    assert len(summary.calls) == 2


def test_summary_rejects_system_role_and_preserves_dispatch_evidence(studio):
    root, store, task = studio
    source, _ = _execute(
        root,
        store,
        task,
        provider=ControlledThreeChoiceProvider(),
        machine_requirements={"decision_option_count": 2},
    )
    assert source["run"]["status"] == "completed"
    prior_runs = {run.id for run in store.list_runs(task.id)}

    class SystemRoleSummary(ControlledSkillSummary):
        def complete(self, **kwargs):
            raw = super().complete(**kwargs)
            value = json.loads(raw[0])
            value["steps"][0]["method"]["operation"] = "validate_decisions"
            return json.dumps(value), *raw[1:]

    summary = SystemRoleSummary()
    with pytest.raises(ValueError, match="执行能力"):
        learn_skill(
            store,
            root,
            "demo",
            source["run_id"],
            operation_id="invalid-system-role",
            provider=summary,
        )
    assert SkillCards(store).list("demo") == []
    new_runs = [run for run in store.list_runs(task.id) if run.id not in prior_runs]
    assert len(new_runs) == 1 and new_runs[0].status == "failed"
    trace_id = new_runs[0].id
    harness = HarnessStore(store)
    requests = harness.requests(trace_id)
    events = harness.events(trace_id, limit=1000)
    assert len(requests) == len(summary.calls) == 1
    sent = json.loads(requests[0]["request"]["user"])
    allowed = sent["output_schema"]["$defs"]["EntryTemplateIR"]["properties"]["operation"]["enum"]
    assert set(allowed) == {
        "",
        "plan_events",
        "draft_quest",
        "logic_contract",
        "dialogue_constrained",
    }
    assert "validate_decisions" not in allowed and "coverage_audit" not in allowed
    assert requests[0]["request"]["config"]["execution_origin"] == "controlled_transport"
    manifest = requests[0]["context_manifest"]
    assert manifest["protocol"] == "skill-card-summary-v2"
    with store.connection() as db:
        receipt = json.loads(
            db.execute(
                "SELECT payload FROM budget_attempt_receipts WHERE id=?",
                (manifest["summary_execution_receipt_id"],),
            ).fetchone()[0]
        )
    assert receipt["kind"] == "execution"
    state = BudgetLedger(store).execution_state(receipt["operation_key"])
    assert state["status"] == "completed_failed" and len(state["attempts"]) == 1
    assert any(event["type"] == "skill/summary_failed" for event in events)
    dispatch_observation = summarize_dispatches(requests, events)
    assert dispatch_observation["dispatch_count"] == 1
    assert dispatch_observation["known_totals"]["input_tokens"] == 17
    assert dispatch_observation["known_totals"]["output_tokens"] == 9
    assert dispatch_observation["cost_usd"] is None
    repeated = learn_skill(
        store,
        root,
        "demo",
        source["run_id"],
        operation_id="do-not-reset-failed-owner",
        provider=summary,
    )
    assert repeated["new_dispatch"] is False
    assert len(summary.calls) == 1 and harness.requests(trace_id) == requests
    assert store.attempts(requests[0]["budget_id"]) == 1

    class NoProductionResult(ControlledThreeChoiceProvider):
        def complete(self, **_):
            raise ConnectionError("Controlled provider-only failure")

    failed_task = _new_task(root, store, "Controlled provider failure before any usable result")
    failed, _ = _execute(root, store, failed_task, provider=NoProductionResult())
    assert failed["run"]["status"] in {"failed", "completed_with_skips"}
    assert not failed.get("feedback_source")
    assert not any(row.get("outputs") for row in failed["result"]["outcomes"].values())
    before = {run.id for run in store.list_runs(failed_task.id)}
    skipped = learn_skill(
        store,
        root,
        "demo",
        failed["run_id"],
        operation_id="do-not-summarize-provider-failure",
        provider=summary,
    )
    assert skipped["status"] == "no_change" and skipped["new_dispatch"] is False
    assert {run.id for run in store.list_runs(failed_task.id)} == before
    assert len(summary.calls) == 1 and SkillCards(store).list("demo") == []
