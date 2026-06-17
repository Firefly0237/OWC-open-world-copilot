"""Design-readiness analysis: deterministic "is this content ready to ship?" scoring.

Distinct from the audit (correctness). See ``project_docs/开发全过程.md``.
"""

from __future__ import annotations

from .models import CheckResult, ItemReadiness, KindSummary, ReadinessReport
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
    "ItemReadiness",
    "KindSummary",
    "ReadinessReport",
    "assess_character",
    "assess_dialogue_tree",
    "assess_quest",
    "assess_readiness",
    "assess_region",
]
