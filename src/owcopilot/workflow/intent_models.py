"""Versioned author intent and isolated patch-review contracts, separate from comparison input."""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..logic.temporal import TemporalSpec
from .models import new_id


def intent_pointer_parts(path: str) -> tuple[str, ...]:
    """Decode strict non-root pointers; traversal checks array indices separately."""
    from ..patches.pointer import pointer_parts

    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise ValueError("意图范围必须是含合法转义的非根JSON Pointer")
    return tuple(pointer_parts(path))


class _IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictIntentTemporalSpec(TemporalSpec):
    model_config = ConfigDict(extra="forbid")


class IntentFieldLock(_IntentModel):
    path: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        intent_pointer_parts(value)
        return value


class IntentFormalRule(_IntentModel):
    quest_id: str = Field(min_length=1)
    spec: StrictIntentTemporalSpec


class IntentContract(_IntentModel):
    """Confirmation and version-chain authority are checked by the saving API, never inferred."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=new_id, min_length=1)
    version: int = Field(default=1, ge=1, strict=True)
    supersedes_contract_id: str | None = None
    project: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_revision: int = Field(ge=1, strict=True)
    baseline_artifact_id: str = Field(min_length=1)
    baseline_artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = Field(default=False, strict=True)
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    hard_locks: list[IntentFieldLock] = Field(default_factory=list, max_length=100)
    formal_rules: list[IntentFormalRule] = Field(default_factory=list, max_length=30)
    soft_text: list[str] = Field(default_factory=list, max_length=30)
    target_error_fingerprints: list[str] = Field(default_factory=list, max_length=30)
    modification_goal: str = Field(default="", max_length=8000)

    @field_validator("allowed_paths")
    @classmethod
    def valid_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            intent_pointer_parts(path)
        if len(set(paths)) != len(paths):
            raise ValueError("允许修改范围不能重复")
        return paths

    @model_validator(mode="after")
    def version_and_goal(self) -> Self:
        if self.version == 1 and self.supersedes_contract_id is not None:
            raise ValueError("首版合同不能覆盖旧合同")
        if self.version > 1 and not self.supersedes_contract_id:
            raise ValueError("修改意图必须以新版本明确supersedes旧合同")
        if self.supersedes_contract_id == self.id:
            raise ValueError("合同不能覆盖自身")
        if any(not value.strip() for value in self.target_error_fingerprints):
            raise ValueError("目标错误指纹不能为空")
        if not self.target_error_fingerprints and not self.modification_goal.strip():
            raise ValueError("必须提供真实目标错误指纹或修改目标原话")
        return self


class IntentPatchReview(_IntentModel):
    contract_id: str
    contract_version: int
    baseline_artifact_id: str
    status: Literal["blocked", "needs_confirmation", "ready_for_author"] = "blocked"
    reasons: list[str] = Field(default_factory=list)
    modification_goal: str = ""
    goal_status: Literal["withheld"] = "withheld"
    changed_paths: list[str] = Field(default_factory=list)
    resolved_errors: list[str] = Field(default_factory=list)
    remaining_targets: list[str] = Field(default_factory=list)
    introduced_errors: list[str] = Field(default_factory=list)
    formal_results: list[dict[str, Any]] = Field(default_factory=list)
    soft_results: list[dict[str, str]] = Field(default_factory=list)
    before_hash: str = ""
    after_hash: str = ""
    candidate_bundle: dict[str, Any] | None = None
    limitations: list[str] = Field(
        default_factory=lambda: [
            "ready_for_author仅表示技术门禁通过，尚未确认自然语言目标或批准正典",
            "正式性质仅适用现有单任务默认初态模型，不扩张比较、映射或循环语义",
        ]
    )
