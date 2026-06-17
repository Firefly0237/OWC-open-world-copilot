"""Design-readiness domain models.

Readiness answers "is this content item finished enough to ship?" — distinct from the audit,
which answers "is it correct?". A failing check here is an incomplete item, not a bug, so these
models never enter the issue stream and never affect the audit's error counts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    """One item on a content type's production-ready checklist."""

    key: str
    label: str
    passed: bool
    detail: str = ""


class ItemReadiness(BaseModel):
    """Readiness of a single content item against its checklist."""

    ref: str
    kind: str
    name: str
    score: float
    ready: bool
    checks: list[CheckResult] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class KindSummary(BaseModel):
    kind: str
    total: int
    ready: int
    average_score: float


class ReadinessReport(BaseModel):
    """Project-level readiness snapshot — a planning-management dashboard, computed on demand."""

    standard_version: str
    content_hash: str
    total_items: int
    ready_items: int
    overall_score: float
    ready_rate: float
    by_kind: list[KindSummary] = Field(default_factory=list)
    items: list[ItemReadiness] = Field(default_factory=list)
