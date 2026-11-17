"""Design-readiness domain models.

Readiness answers "is this content item finished enough to ship?" — distinct from the audit,
which answers "is it correct?". A failing check here is an incomplete item, not a bug, so these
models never enter the issue stream and never affect the audit's error counts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    """One item on a content type's completeness checklist."""

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


class DanglingInspirationRef(BaseModel):
    """One canon object whose ``metadata.inspiration_refs`` cites reference-library chunks that no
    longer exist (source deleted, or a re-ingest reshuffled chunk boundaries) — a stale citation,
    not a correctness bug: the object's own content is unaffected, but its "grounded in X" claim
    can no longer be verified. See ``inspiration.lineage.dangling_inspiration_refs`` (N2.4 / B7)."""

    object_ref: str
    dangling_refs: list[str] = Field(default_factory=list)


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
    # Additive, opt-in: only populated when the caller passes known_reference_chunk_ids to
    # assess_readiness(). Every existing caller that doesn't leaves this empty — unchanged
    # behaviour, unchanged byte shape for anyone not asking for the check.
    dangling_inspiration_refs: list[DanglingInspirationRef] = Field(default_factory=list)
