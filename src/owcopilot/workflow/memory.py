"""Explicit manual/lexical recall and accepted-method context, without semantic pretence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..inspiration.models import ReferenceSource
from ..retrieval.text_match import lexical_score
from .applicability import assess_applicability
from .models import TaskRecord, WorkflowSpec
from .store import WorkflowStore


def recall_workflows(
    store: WorkflowStore,
    query: str,
    *,
    project: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recall applicable saved workflows with an explicit lexical scoring baseline."""
    from .planning import validate_bindings

    matches = []
    for record in store.workflows():
        if record.get("disabled") or record.get("schema_version") != 1:
            continue
        if record.get("scope") == "project" and record.get("project") != project:
            continue
        try:
            validate_bindings(WorkflowSpec.model_validate(record["spec"]))
        except (KeyError, ValueError):
            continue
        score = lexical_score(query, [record["name"], record.get("description", "")])
        if score > 0:
            matches.append({**record, "score": score, "retrieval": "lexical"})
    return sorted(matches, key=lambda item: -item["score"])[:limit]


def accepted_workflow_context(
    store: WorkflowStore,
    *,
    project: str,
    limit: int = 8,
    max_chars: int = 16000,
    task: TaskRecord | None = None,
    reference_sources: Sequence[ReferenceSource] | None = None,
) -> list[dict[str, Any]]:
    """Recall a bounded accepted library with explicit resource admission."""
    from .planning import validate_bindings

    selected = []
    used = 0
    for record in store.workflows():
        if (
            record.get("disabled")
            or record.get("project") != project
            or record.get("schema_version") != 1
            or record.get("method_origin", record.get("memory_kind")) != "trace_induced"
            or record.get("technical_status") != "validated"
            or record.get("acceptance_status") != "accepted"
        ):
            continue
        try:
            spec = validate_bindings(WorkflowSpec.model_validate(record["spec"]))
        except (KeyError, ValueError):
            continue
        if task is not None:
            if task.project != project:
                raise ValueError("方法上下文与任务项目不一致")
            assessment = assess_applicability(spec, task, reference_sources=reference_sources)
            if not assessment["compatible"]:
                continue
        hint = {
            "id": record["id"],
            "name": record["name"],
            "spec": spec.model_dump(mode="json"),
            "source_run_id": record.get("source_run_id"),
            "memory_kind": "trace_induced",
            "retrieval": "accepted_library_context",
        }
        size = len(json.dumps(hint, ensure_ascii=False))
        if used + size > max_chars:
            continue
        selected.append(hint)
        used += size
        if len(selected) >= limit:
            break
    return selected
