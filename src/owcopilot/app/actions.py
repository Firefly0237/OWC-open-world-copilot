"""UI actions that execute project workflow steps without importing Streamlit."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..exporters import EngineTarget, export_content_bundle
from ..pipeline.audit import run_full_audit
from ..pipeline.project import ProjectContext
from ..telemetry import deterministic_step, summarize_workflow


def run_project_audit_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        result = run_full_audit(project, persist=persist)
        return {
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "open_errors": len(result.open_errors),
            "cost_budget": _deterministic_cost_budget("audit_project"),
        }


def run_project_export_action(
    content_root: str | Path,
    *,
    output_dir: str | Path,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    engine = EngineTarget(target_engine)
    actual_output = Path(output_dir) / engine.value
    with _project(content_root, sqlite_path) as project:
        manifest = export_content_bundle(project.bundle, actual_output, target_engine=engine)
        return {
            "output_dir": str(actual_output),
            "manifest": manifest.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("export_project"),
        }


@contextmanager
def _project(content_root: str | Path, sqlite_path: str | None) -> Iterator[ProjectContext]:
    root = Path(content_root)
    if not root.exists():
        raise FileNotFoundError(f"content root does not exist: {root}")
    runtime_path = Path(sqlite_path) if sqlite_path else root / ".owcopilot" / "runtime.sqlite"
    if str(runtime_path) != ":memory:":
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
    project = ProjectContext.open(root, sqlite_path=runtime_path)
    try:
        yield project
    finally:
        project.close()


def _deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")
