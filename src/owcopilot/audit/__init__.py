# v2 确定性审计框架：本项目的 ground-truth oracle。
"""Deterministic audit framework for v2."""

from .baseline import AuditBaseline, apply_baseline, issue_fingerprint
from .context import AuditContext
from .default_rules import build_default_rule_registry
from .models import AuditRun, Category, Evidence, Issue, IssueStatus, Severity
from .registry import RuleConfig, RuleRegistry
from .rule import Rule
from .runner import AuditResult, AuditRunner
from .source_location import (
    ContentSourceLocator,
    SourceLocation,
    annotate_issues,
    content_file_for,
    node_ref_from_path,
)

__all__ = [
    "AuditBaseline",
    "AuditContext",
    "AuditResult",
    "AuditRun",
    "AuditRunner",
    "Category",
    "ContentSourceLocator",
    "Evidence",
    "Issue",
    "IssueStatus",
    "Rule",
    "RuleConfig",
    "RuleRegistry",
    "Severity",
    "SourceLocation",
    "annotate_issues",
    "apply_baseline",
    "build_default_rule_registry",
    "content_file_for",
    "issue_fingerprint",
    "node_ref_from_path",
]
