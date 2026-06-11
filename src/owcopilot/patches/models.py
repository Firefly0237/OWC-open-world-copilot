"""Patch models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import Origin


class PatchOp(str, Enum):
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


class PatchStatus(str, Enum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class PatchOperation(BaseModel):
    op: PatchOp
    path: str
    value: Any = None


class PatchCandidate(BaseModel):
    id: str | None = None
    issue_id: str | None = None
    ops: list[PatchOperation] = Field(min_length=1)
    rationale: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    origin: Origin = Origin.AI_PATCH
    status: PatchStatus = PatchStatus.PROPOSED
