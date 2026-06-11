"""Audit domain models shared by rules, reports, API and CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(str, Enum):
    IMPORT = "import"
    REFERENCE = "reference"
    GRAPH = "graph"
    LORE = "lore"
    REGION = "region"
    PIPELINE = "pipeline"
    STYLE = "style"
    TRUST = "trust"


class IssueStatus(str, Enum):
    OPEN = "open"
    SUPPRESSED = "suppressed"
    FIXED = "fixed"


class Evidence(BaseModel):
    kind: str
    target_ref: str | None = None
    path: str | None = None
    relation: tuple[str, str, str] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Issue(BaseModel):
    id: str | None = None
    rule_code: str
    severity: Severity = Severity.ERROR
    category: Category
    target_ref: str
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    fingerprint: str | None = None
    audit_run_id: str | None = None
    status: IssueStatus = IssueStatus.OPEN


class AuditRun(BaseModel):
    id: str
    content_hash: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rule_set_version: str = "v2.0"
    totals: dict[str, int] = Field(default_factory=dict)
    baseline_delta: dict[str, int] = Field(default_factory=dict)
