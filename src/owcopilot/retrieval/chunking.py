"""Structure-constrained semantic chunking for immutable intake ``SourceSegment`` rows.

The chunker separates three concerns that are commonly conflated:

* an intake segment is the smallest source-locatable atom;
* a parent is a hard-structure group that retrieval may expand for generation;
* a child is the bounded unit indexed for lexical/dense recall.

Semantic similarity may choose only between adjacent legal atoms.  It can never cross an artifact,
table, worksheet or locator-kind boundary.  A hashing embedder is deliberately rejected in
``semantic`` mode: deterministic hashing remains a useful test/baseline, but is not semantic
chunking.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..intake.contracts import SourceSegment
from ..llm.cache import Embedder
from .budget import estimate_tokens

RETRIEVAL_DOCUMENT_SCHEMA_VERSION = "retrieval-document-v1"
DEFAULT_CHUNK_POLICY_VERSION = "structure-semantic-parent-child-v1"
_ID_NAMESPACE = UUID("7c831dc9-b22b-49cc-98cf-f8fc9ae7a23d")


class SemanticChunkingUnavailable(RuntimeError):
    """The requested semantic strategy has no real semantic embedding backend."""


class ChunkingPolicy(BaseModel):
    """Frozen, versioned parameters selected on dev data before held-out evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = DEFAULT_CHUNK_POLICY_VERSION
    strategy: Literal["structure", "semantic"] = "semantic"
    min_tokens: int = 80
    target_tokens: int = 220
    max_tokens: int = 360
    overlap_tokens: int = 40
    semantic_breakpoint: float = 0.58

    @model_validator(mode="after")
    def validate_ranges(self) -> ChunkingPolicy:
        if not self.version.strip():
            raise ValueError("chunk policy version must not be blank")
        if self.min_tokens < 1:
            raise ValueError("min_tokens must be >= 1")
        if not self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("chunk token limits must satisfy min <= target <= max")
        if self.overlap_tokens < 0 or self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be >= 0 and smaller than max_tokens")
        if not -1.0 <= self.semantic_breakpoint <= 1.0:
            raise ValueError("semantic_breakpoint must be within cosine range [-1, 1]")
        return self


class RetrievalParent(BaseModel):
    """A hard-structure group retained for parent/window expansion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parent_id: str
    structure_kind: str
    text: str
    text_hash: str
    token_count: int
    segment_ids: tuple[str, ...]
    locator_start: dict[str, object]
    locator_end: dict[str, object]


class RetrievalUnit(BaseModel):
    """A bounded child indexed for recall while retaining exact source provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: str
    artifact_version_id: str
    parent_id: str
    chunk_index: int
    text: str
    text_hash: str
    token_count: int
    segment_ids: tuple[str, ...]
    locators: tuple[dict[str, object], ...]
    ordinal_start: int
    ordinal_end: int
    policy_version: str
    strategy: Literal["structure", "semantic"]
    embedding_model_id: str | None = None
    token_counter_id: str
    overlap_from_previous: tuple[str, ...] = ()
    semantic_break_score: float | None = None
    oversized: bool = False


class RetrievalDocument(BaseModel):
    """Versioned sidecar binding one artifact to its parent/child retrieval units."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["retrieval-document-v1"] = "retrieval-document-v1"
    artifact_version_id: str
    policy: ChunkingPolicy
    embedding_model_id: str | None = None
    token_counter_id: str
    parents: tuple[RetrievalParent, ...] = Field(min_length=1)
    units: tuple[RetrievalUnit, ...] = Field(min_length=1)


@dataclass(frozen=True)
class _Atom:
    text: str
    segments: tuple[SourceSegment, ...]
    token_count: int
    hard_key: tuple[object, ...]
    structure_kind: str


def build_retrieval_document(
    segments: Sequence[SourceSegment],
    *,
    policy: ChunkingPolicy | None = None,
    embedder: Embedder | None = None,
    token_counter: Callable[[str], int] = estimate_tokens,
    token_counter_id: str = "owcopilot-estimate-tokens-v1",
) -> RetrievalDocument:
    """Build parent/child units without losing or reordering any source segment.

    ``semantic`` mode requires an embedder whose live ``model_id`` begins with ``st:`` both before
    and after encoding.  ``SemanticEmbedder`` changes its id when it degrades, so a failed model
    load cannot silently turn this result into hashing-based "semantic" chunks.
    """

    selected = policy or ChunkingPolicy()
    ordered = _validate_segments(segments)
    atoms = _build_atoms(ordered, token_counter=token_counter)

    semantic_vectors: list[list[float]] | None = None
    embedding_model_id: str | None = None
    if selected.strategy == "semantic":
        if embedder is None or not str(embedder.model_id).startswith("st:"):
            raise SemanticChunkingUnavailable(
                "semantic chunking requires a real sentence-transformer embedder (model_id=st:*)"
            )
        semantic_vectors = embedder.embed_many([atom.text for atom in atoms])
        if not str(embedder.model_id).startswith("st:"):
            raise SemanticChunkingUnavailable(
                "semantic embedder degraded during encoding; structure-only fallback must be "
                "selected explicitly"
            )
        if len(semantic_vectors) != len(atoms):
            raise ValueError("embedder returned a different number of vectors than input atoms")
        embedding_model_id = str(embedder.model_id)

    parents: list[RetrievalParent] = []
    units: list[RetrievalUnit] = []
    chunk_index = 0
    group_start = 0
    while group_start < len(atoms):
        group_end = group_start + 1
        while group_end < len(atoms) and atoms[group_end].hard_key == atoms[group_start].hard_key:
            group_end += 1
        group = atoms[group_start:group_end]
        parent = _parent_for(group, token_counter=token_counter)
        parents.append(parent)
        similarities = _adjacent_similarities(
            None if semantic_vectors is None else semantic_vectors[group_start:group_end]
        )
        child_spans = _chunk_spans(group, similarities=similarities, policy=selected)
        previous_segment_ids: set[str] = set()
        for local_start, local_end, break_score in child_spans:
            selected_atoms = group[local_start:local_end]
            child_segments = tuple(
                segment for atom in selected_atoms for segment in atom.segments
            )
            text = _join_atom_text(selected_atoms)
            segment_ids = tuple(segment.segment_id for segment in child_segments)
            unit_id = str(
                uuid5(
                    _ID_NAMESPACE,
                    ":".join(
                        (
                            ordered[0].artifact_version_id,
                            selected.version,
                            parent.parent_id,
                            *segment_ids,
                        )
                    ),
                )
            )
            units.append(
                RetrievalUnit(
                    unit_id=unit_id,
                    artifact_version_id=ordered[0].artifact_version_id,
                    parent_id=parent.parent_id,
                    chunk_index=chunk_index,
                    text=text,
                    text_hash=_text_hash(text),
                    token_count=max(1, token_counter(text)),
                    segment_ids=segment_ids,
                    locators=tuple(
                        segment.locator.model_dump(mode="json") for segment in child_segments
                    ),
                    ordinal_start=child_segments[0].ordinal,
                    ordinal_end=child_segments[-1].ordinal,
                    policy_version=selected.version,
                    strategy=selected.strategy,
                    embedding_model_id=embedding_model_id,
                    token_counter_id=token_counter_id,
                    overlap_from_previous=tuple(
                        segment_id
                        for segment_id in segment_ids
                        if segment_id in previous_segment_ids
                    ),
                    semantic_break_score=break_score,
                    oversized=sum(atom.token_count for atom in selected_atoms)
                    > selected.max_tokens,
                )
            )
            previous_segment_ids = set(segment_ids)
            chunk_index += 1
        group_start = group_end

    return RetrievalDocument(
        artifact_version_id=ordered[0].artifact_version_id,
        policy=selected,
        embedding_model_id=embedding_model_id,
        token_counter_id=token_counter_id,
        parents=tuple(parents),
        units=tuple(units),
    )


def _validate_segments(segments: Sequence[SourceSegment]) -> list[SourceSegment]:
    if not segments:
        raise ValueError("at least one source segment is required")
    ordered = sorted(segments, key=lambda segment: segment.ordinal)
    artifact_ids = {segment.artifact_version_id for segment in ordered}
    if len(artifact_ids) != 1:
        raise ValueError("one retrieval document cannot mix artifact versions")
    ordinals = [segment.ordinal for segment in ordered]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("source segment ordinals must be unique")
    if len({segment.segment_id for segment in ordered}) != len(ordered):
        raise ValueError("source segment ids must be unique")
    return ordered


def _build_atoms(
    segments: Sequence[SourceSegment], *, token_counter: Callable[[str], int]
) -> list[_Atom]:
    atoms: list[_Atom] = []
    pending: list[SourceSegment] = []
    pending_atomic_key: tuple[object, ...] | None = None
    for segment in segments:
        atomic_key = _atomic_key(segment)
        if pending and atomic_key != pending_atomic_key:
            atoms.append(_make_atom(pending, token_counter=token_counter))
            pending = []
        pending.append(segment)
        pending_atomic_key = atomic_key
    if pending:
        atoms.append(_make_atom(pending, token_counter=token_counter))
    return atoms


def _atomic_key(segment: SourceSegment) -> tuple[object, ...]:
    locator = segment.locator
    if locator.kind == "docx_table_cell":
        return (locator.kind, locator.table_index, locator.row_index)
    if locator.kind == "xlsx_cell":
        return (locator.kind, locator.sheet_name, locator.row_index)
    return (locator.kind, segment.segment_id)


def _hard_key(segment: SourceSegment) -> tuple[tuple[object, ...], str]:
    locator = segment.locator
    prefix: tuple[object, ...] = (segment.artifact_version_id, locator.kind)
    if locator.kind == "docx_table_cell":
        return (*prefix, locator.table_index), f"docx_table:{locator.table_index}"
    if locator.kind == "xlsx_cell":
        return (*prefix, locator.sheet_name), f"xlsx_sheet:{locator.sheet_name}"
    if locator.kind == "pdf_text_line":
        # A physical page turn is not automatically a semantic/permission boundary. Keeping one
        # PDF text-flow parent allows a definition or quest condition to continue across pages;
        # every child still carries each page/bbox locator for exact citation.
        return prefix, "pdf_text_flow"
    return prefix, "docx_body"


def _make_atom(
    segments: Sequence[SourceSegment], *, token_counter: Callable[[str], int]
) -> _Atom:
    hard_key, structure_kind = _hard_key(segments[0])
    text = " | ".join(segment.text for segment in segments)
    return _Atom(
        text=text,
        segments=tuple(segments),
        token_count=max(1, token_counter(text)),
        hard_key=hard_key,
        structure_kind=structure_kind,
    )


def _parent_for(
    atoms: Sequence[_Atom], *, token_counter: Callable[[str], int]
) -> RetrievalParent:
    segments = tuple(segment for atom in atoms for segment in atom.segments)
    text = _join_atom_text(atoms)
    parent_id = str(
        uuid5(
            _ID_NAMESPACE,
            json.dumps(
                {
                    "artifact": segments[0].artifact_version_id,
                    "hard_key": atoms[0].hard_key,
                    "segment_ids": [segment.segment_id for segment in segments],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
    )
    return RetrievalParent(
        parent_id=parent_id,
        structure_kind=atoms[0].structure_kind,
        text=text,
        text_hash=_text_hash(text),
        token_count=max(1, token_counter(text)),
        segment_ids=tuple(segment.segment_id for segment in segments),
        locator_start=segments[0].locator.model_dump(mode="json"),
        locator_end=segments[-1].locator.model_dump(mode="json"),
    )


def _chunk_spans(
    atoms: Sequence[_Atom],
    *,
    similarities: Sequence[float] | None,
    policy: ChunkingPolicy,
) -> list[tuple[int, int, float | None]]:
    spans: list[tuple[int, int, float | None]] = []
    start = 0
    while start < len(atoms):
        end = start
        tokens = 0
        candidate_cuts: list[tuple[float, int]] = []
        chosen_end: int | None = None
        while end < len(atoms):
            next_tokens = tokens + atoms[end].token_count
            if end > start and next_tokens > policy.max_tokens:
                if candidate_cuts:
                    chosen_end = min(candidate_cuts, key=lambda item: (item[0], item[1]))[1]
                else:
                    chosen_end = end
                break
            tokens = next_tokens
            end += 1
            if end == len(atoms):
                chosen_end = end
                break
            if tokens < policy.min_tokens:
                continue
            distance_penalty = 0.15 * abs(tokens - policy.target_tokens) / policy.target_tokens
            boundary_similarity = similarities[end - 1] if similarities is not None else 1.0
            candidate_cuts.append((boundary_similarity + distance_penalty, end))
            if policy.strategy == "structure" and tokens >= policy.target_tokens:
                chosen_end = end
                break
            if (
                policy.strategy == "semantic"
                and tokens >= policy.target_tokens
                and boundary_similarity <= policy.semantic_breakpoint
            ):
                chosen_end = end
                break

        if chosen_end is None:
            chosen_end = max(start + 1, end)
        break_score = (
            similarities[chosen_end - 1]
            if similarities is not None and chosen_end < len(atoms)
            else None
        )
        spans.append((start, chosen_end, break_score))
        if chosen_end >= len(atoms):
            break
        next_start = chosen_end
        overlap = 0
        cursor = chosen_end - 1
        while cursor > start:
            candidate = overlap + atoms[cursor].token_count
            if candidate > policy.overlap_tokens:
                break
            overlap = candidate
            next_start = cursor
            cursor -= 1
        start = next_start if next_start > start else chosen_end
    return spans


def _adjacent_similarities(vectors: Sequence[Sequence[float]] | None) -> list[float] | None:
    if vectors is None:
        return None
    return [_cosine(left, right) for left, right in zip(vectors, vectors[1:], strict=False)]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("semantic vectors must have the same non-zero dimension")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _join_atom_text(atoms: Sequence[_Atom]) -> str:
    return "\n".join(atom.text for atom in atoms).strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "ChunkingPolicy",
    "DEFAULT_CHUNK_POLICY_VERSION",
    "RETRIEVAL_DOCUMENT_SCHEMA_VERSION",
    "RetrievalDocument",
    "RetrievalParent",
    "RetrievalUnit",
    "SemanticChunkingUnavailable",
    "build_retrieval_document",
]
