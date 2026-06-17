"""Read an existing game-project file and recognize its entities + relationships.

Deterministic-first, LLM-assisted (default off, §8-guarded), review-gated: every run produces an
editable ``ImportPlan`` a human can correct before anything reaches canon. MVP formats: spreadsheets
(unknown columns + foreign-key inference) and articy:draft JSON exports.
"""

from __future__ import annotations

from .articy import recognize_articy
from .engine_data import recognize_engine_data
from .ink import recognize_ink
from .llm_relations import (
    build_llm_relation_proposer,
    evidence_grounded,
    propose_relations_guarded,
)
from .models import (
    ColumnMapping,
    ImportPlan,
    ProposedEntity,
    ProposedRelation,
    SourceRef,
)
from .offline import OfflineRelationProvider
from .pipeline import SUPPORTED_FORMATS, diff_against_canon, plan_to_bundle, recognize
from .sniff import sniff_source_format
from .table import infer_table_mapping, recognize_table
from .yarn import recognize_yarn

__all__ = [
    "SUPPORTED_FORMATS",
    "ColumnMapping",
    "ImportPlan",
    "OfflineRelationProvider",
    "ProposedEntity",
    "ProposedRelation",
    "SourceRef",
    "build_llm_relation_proposer",
    "diff_against_canon",
    "evidence_grounded",
    "infer_table_mapping",
    "plan_to_bundle",
    "propose_relations_guarded",
    "recognize",
    "recognize_articy",
    "recognize_engine_data",
    "recognize_ink",
    "recognize_table",
    "recognize_yarn",
    "sniff_source_format",
]
