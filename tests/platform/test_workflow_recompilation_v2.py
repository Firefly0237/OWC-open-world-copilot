"""Empty/prepared runs cannot be relabelled as real stored-IR recompilations."""

import pytest

from owcopilot.workflow import v2_service
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.harness import HarnessStore
from owcopilot.workflow.recompilation_v2 import execute_localization_recompile
from tests.platform import test_workflow_v2_service as source_fixtures

studio = source_fixtures.studio


def test_recompile_rejects_missing_real_source_before_creating_trial_or_dispatch(studio):
    root, store, task = studio
    prepared = v2_service.prepare_v2_run(
        store,
        root,
        task,
        create_narrative_graph().model_dump(mode="json"),
    )
    with store.connection() as db:
        before = db.execute("SELECT count(*) FROM runs").fetchone()[0]
    with pytest.raises(ValueError, match="完整真实产物"):
        execute_localization_recompile(
            store,
            root,
            "demo",
            prepared["run_id"],
            expected_source_graph_hash="a" * 64,
            expected_source_bound_hash="b" * 64,
        )
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == before
    assert HarnessStore(store).requests(prepared["run_id"]) == []
