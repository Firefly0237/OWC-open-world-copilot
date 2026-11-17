# 设计就绪度分析：确定性的『这内容能发了吗』评分。
"""Design-readiness analysis: deterministic "is this content ready to ship?" scoring.

Distinct from the audit: readiness measures completeness, while audit rules measure correctness.
"""

from __future__ import annotations

from .models import CheckResult, DanglingInspirationRef, ItemReadiness, KindSummary, ReadinessReport
from .service import (
    STANDARD_VERSION,
    assess_character,
    assess_dialogue_tree,
    assess_quest,
    assess_readiness,
    assess_region,
)

__all__ = [
    "STANDARD_VERSION",
    "CheckResult",
    "DanglingInspirationRef",
    "ItemReadiness",
    "KindSummary",
    "ReadinessReport",
    "assess_character",
    "assess_dialogue_tree",
    "assess_quest",
    "assess_readiness",
    "assess_region",
]
