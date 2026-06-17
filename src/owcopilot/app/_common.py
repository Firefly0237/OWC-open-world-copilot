"""Helpers shared by the app's action and view-model layers.

Both layers open a project the same way and surface the same deterministic ($0) cost stub; these
lived as byte-identical copies in ``actions.py`` and ``view_models.py`` (a split out of the old
``dashboard.py``) and so were a standing drift risk. Single-sourcing them here keeps the two
front-ends opening projects identically.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..pipeline.project import ProjectContext
from ..telemetry import deterministic_step, summarize_workflow

# Published while a project is open so a gateway built inside the block (via ``actions._gateway``)
# can scope its cache to this project — without every action threading a namespace argument through
# by hand. Keeping the project identity in the cache key is what stops one project's generated
# content from being served to another that happens to share the same brief/grounding (see
# ``CacheKey.namespace``). View-model reads never build a gateway, so for them it is simply unused.
PROJECT_NAMESPACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "owcopilot_project_namespace", default=""
)


@contextmanager
def open_project(content_root: str | Path, sqlite_path: str | None) -> Iterator[ProjectContext]:
    """Open a project's content + runtime store, publish its cache namespace, and close on exit."""
    root = Path(content_root)
    if not root.exists():
        raise FileNotFoundError(f"content root does not exist: {root}")
    runtime_path = Path(sqlite_path) if sqlite_path else root / ".owcopilot" / "runtime.sqlite"
    if str(runtime_path) != ":memory:":
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
    project = ProjectContext.open(root, sqlite_path=runtime_path)
    token = PROJECT_NAMESPACE.set(str(root))
    try:
        yield project
    finally:
        PROJECT_NAMESPACE.reset(token)
        project.close()


def deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    """The cost budget for a deterministic ($0) step — exact, never an estimate."""
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")
