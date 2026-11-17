"""Executable operation parameters; planning annotations are not executable instructions."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .models import Contract, WorkflowSpec


class _NoParameters(Contract):
    pass


class _ContextParameters(Contract):
    budget_tokens: int = Field(default=1500, ge=1, le=8000, strict=True)


class _DialogueParameters(Contract):
    max_nodes: int = Field(default=8, ge=1, le=24, strict=True)
    max_chars: int = Field(default=160, ge=1, le=600, strict=True)


class _ComparisonParameters(Contract):
    baseline_quest_id: str | None = None


PARAMETER_MODELS: dict[str, type[Contract]] = {
    "plan_events": _ContextParameters,
    "draft_quest": _ContextParameters,
    "draft_dialogue": _DialogueParameters,
    "draft_logic": _NoParameters,
    "bind_dialogue": _NoParameters,
    "audit": _NoParameters,
    "compare": _ComparisonParameters,
    "package": _NoParameters,
}

# Absent entries retain the legacy cache signature. Bump only an operation whose
# executable contract changed; completed runs remain immutable historical evidence.
OPERATION_CONTRACT_VERSIONS: dict[str, int] = {"draft_quest": 2}


def operation_parameter_schema(operation: str) -> dict[str, Any]:
    return PARAMETER_MODELS[operation].model_json_schema()


def validate_executable_parameters(spec: WorkflowSpec) -> WorkflowSpec:
    """New plans must not hide unconsumed creative instructions in arbitrary dictionaries."""
    for node in spec.nodes:
        PARAMETER_MODELS[node.operation].model_validate(node.parameters)
    return spec
