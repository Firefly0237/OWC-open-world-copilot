# 从非结构化文稿（小说/剧本/笔记）提炼可审阅内容草稿。
"""Distill unstructured manuscripts (novels, scripts, notes) into reviewable content drafts."""

from .models import CoverageReport, ExtractionDraft, ExtractionGap, PlotBeat
from .offline import OfflineExtractionProvider, OfflineGapFillProvider
from .service import (
    ExtractionService,
    apply_gap_answers,
    chunk_text,
    decode_document_bytes,
    parse_extraction_payload,
    plan_coverage,
    quests_from_beats,
)

__all__ = [
    "CoverageReport",
    "ExtractionDraft",
    "ExtractionGap",
    "ExtractionService",
    "OfflineExtractionProvider",
    "OfflineGapFillProvider",
    "PlotBeat",
    "apply_gap_answers",
    "chunk_text",
    "decode_document_bytes",
    "parse_extraction_payload",
    "plan_coverage",
    "quests_from_beats",
]
