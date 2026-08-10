# 灵感参考库。
"""Inspiration reference library.

Reference materials live beside a content project but outside the canonical content bundle.
They are searchable for creative generation, never mixed into lore QA by default.
"""

from .grounding import (
    build_reference_report,
    inspiration_prompt_section,
    inspiration_refs_metadata,
    retrieve_drafting_references,
)
from .leakage import (
    STANDARD_THRESHOLDS,
    STRICT_THRESHOLDS,
    ReferenceLeakage,
    declared_names,
    detect_reference_leakage,
    draft_prose,
)
from .lineage import dangling_inspiration_refs, objects_citing_chunks
from .models import (
    PROVENANCE_LABELS,
    REFERENCE_USES,
    USE_DRAFTING,
    USE_INSPIRATION,
    Provenance,
    ReferenceChunk,
    ReferenceIngestResult,
    ReferenceSource,
    ensure_canon_intake_allowed,
    parse_provenance,
)
from .report import UNVERIFIED_TRANSFORMATION, ReferenceReportItem
from .retrieval import ReferenceContextBuilder
from .store import ReferenceStore, decode_reference_bytes

__all__ = [
    "PROVENANCE_LABELS",
    "REFERENCE_USES",
    "STANDARD_THRESHOLDS",
    "STRICT_THRESHOLDS",
    "UNVERIFIED_TRANSFORMATION",
    "USE_DRAFTING",
    "USE_INSPIRATION",
    "Provenance",
    "ReferenceChunk",
    "ReferenceContextBuilder",
    "ReferenceIngestResult",
    "ReferenceLeakage",
    "ReferenceReportItem",
    "ReferenceSource",
    "ReferenceStore",
    "build_reference_report",
    "dangling_inspiration_refs",
    "declared_names",
    "decode_reference_bytes",
    "detect_reference_leakage",
    "draft_prose",
    "ensure_canon_intake_allowed",
    "inspiration_prompt_section",
    "inspiration_refs_metadata",
    "objects_citing_chunks",
    "parse_provenance",
    "retrieve_drafting_references",
]
