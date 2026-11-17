"""Loaded role implementation and output-schema identity are frozen before dispatch."""

import pytest

from owcopilot.app.actions.workflow import create_task_action, open_workflow_store
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.workflow import v2_service
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.models import TaskInput


def test_changed_loaded_role_contract_cannot_reuse_or_dispatch_an_old_run(tmp_path, monkeypatch):
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    created = create_task_action(root, "p", TaskInput(brief="Controlled identity check"))
    store = open_workflow_store(root)
    task = store.get_task(created["task"]["id"])
    state = v2_service.prepare_v2_run(
        store, root, task, create_narrative_graph("constrained").model_dump(mode="json")
    )
    original = v2_service.execute_v2_run
    frozen = state["host_contract_identity"]

    def changed_implementation():
        return "changed role instruction"

    monkeypatch.setattr(v2_service, "execute_v2_run", changed_implementation)
    assert v2_service._host_contract_identity(task) != frozen
    with pytest.raises(ValueError, match="schema"):
        original(store, root, "p", state["run_id"], provider=object())
    assert store.attempts(f"{task.id}:1:plan_events") == 0
    assert v2_service.v2_detail(store, "p", state["run_id"])["result"] is None
