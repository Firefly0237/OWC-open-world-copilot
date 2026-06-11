"""Distill unstructured manuscripts (novels, scripts, notes) into reviewable content drafts."""

from .models import ExtractionDraft, ExtractionGap, PlotBeat
from .offline import OfflineExtractionProvider, OfflineGapFillProvider
from .service import (
    ExtractionService,
    apply_gap_answers,
    chunk_text,
    decode_document_bytes,
    parse_extraction_payload,
    quests_from_beats,
)

__all__ = [
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
    "quests_from_beats",
]
