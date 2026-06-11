from __future__ import annotations

from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.main import PipelineStage, run_audit_workflow
from owcopilot.pipeline.project import ProjectContext


def test_run_audit_workflow_returns_stage_summary_and_audit_result(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle(quests={"q1": Quest(id="q1", title="Q1")}))
    project = ProjectContext.open(content_root)
    try:
        result = run_audit_workflow(project)

        assert result.summary.completed == [PipelineStage.INDEX, PipelineStage.AUDIT]
        assert result.summary.notes == [f"open_errors={len(result.audit.open_errors)}"]
        assert project.sqlite_store.get_audit_run(result.audit.run.id) is not None
    finally:
        project.close()
