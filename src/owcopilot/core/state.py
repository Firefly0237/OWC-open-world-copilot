"""Graph state for the PLAN-EXECUTE-VERIFY orchestration loop.

We use a TypedDict as the LangGraph state schema (the well-trodden path, with an
accumulating reducer on `log`). Rich *domain* objects (World Bible, Quest) stay
Pydantic — see `worldbible/models.py` and `generation/quest.py`.
"""

from __future__ import annotations

from enum import Enum
from operator import add
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel


class Phase(str, Enum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"
    DONE = "DONE"
    FAILED = "FAILED"


class ValidationIssue(BaseModel):
    """A single consistency problem found by a validator."""

    code: str
    message: str
    severity: str = "error"  # "error" | "warning"
    entity_ref: str | None = None


class TaskState(TypedDict, total=False):
    """Shared state threaded through the graph.

    `log` uses the `add` reducer so each node *appends* lines instead of overwriting.
    Everything else uses the default (last-write-wins) reducer.
    """

    intent: str
    phase: Phase
    plan: list[str]
    artifact: dict[str, Any] | None  # the generated structured content (e.g. a Quest dump)
    issues: list[ValidationIssue]
    repair_attempts: int
    max_repair_attempts: int
    log: Annotated[list[str], add]
