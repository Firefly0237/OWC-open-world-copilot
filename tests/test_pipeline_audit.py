from __future__ import annotations

from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.audit import run_full_audit
from owcopilot.pipeline.project import ProjectContext


def test_run_full_audit_persists_audit_run_and_issues(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )
    project = ProjectContext.open(content_root)
    try:
        result = run_full_audit(project)

        assert result.open_errors
        assert project.sqlite_store.get_audit_run(result.run.id) is not None
        assert project.sqlite_store.list_issues(rule_code="UNKNOWN_ENTITY_REF")
    finally:
        project.close()


def test_run_full_audit_can_skip_persistence(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle(quests={"q1": Quest(id="q1", title="Q1")}))
    project = ProjectContext.open(content_root)
    try:
        result = run_full_audit(project, persist=False)

        assert result.run.id
        assert project.sqlite_store.get_audit_run(result.run.id) is None
    finally:
        project.close()
