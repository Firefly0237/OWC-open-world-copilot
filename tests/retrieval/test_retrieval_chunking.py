from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from owcopilot.inspiration import Provenance, ReferenceContextBuilder, ReferenceStore
from owcopilot.intake import PdfTextLineLocator, SourceSegment
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval import (
    ChunkingPolicy,
    SemanticChunkingUnavailable,
    build_retrieval_document,
)
from owcopilot.storage import SQLiteStore


class _SemanticFixtureEmbedder:
    model_id = "st:semantic-chunk-fixture"

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if "sigil" in text:
                vectors.append([1.0, 0.0])
            elif "archivist" in text:
                vectors.append([0.9, 0.1])
            elif "spaceship" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.1, 0.9])
        return vectors


def _pdf_segments(lines: list[str]) -> list[SourceSegment]:
    artifact_id = str(uuid4())
    segments: list[SourceSegment] = []
    for ordinal, line in enumerate(lines):
        segments.append(
            SourceSegment.from_text(
                segment_id=str(uuid4()),
                artifact_version_id=artifact_id,
                ordinal=ordinal,
                locator=PdfTextLineLocator(
                    page_index=ordinal // 2,
                    line_index=ordinal % 2,
                    x0=72.0,
                    top=72.0 + 24.0 * (ordinal % 2),
                    x1=320.0,
                    bottom=88.0 + 24.0 * (ordinal % 2),
                    page_width=612.0,
                    page_height=792.0,
                    validation_backend_version="5.12.1",
                    extraction_backend_version="0.11.10",
                ),
                text=line,
            )
        )
    return segments


def _word_tokens(text: str) -> int:
    return len(text.split())


def test_semantic_chunking_cuts_only_at_low_similarity_legal_boundary() -> None:
    segments = _pdf_segments(
        [
            "sigil recovery condition",
            "return to archivist",
            "spaceship fuel system",
            "launch from orbit",
        ]
    )
    document = build_retrieval_document(
        segments,
        policy=ChunkingPolicy(
            version="semantic-fixture-v1",
            strategy="semantic",
            min_tokens=2,
            target_tokens=4,
            max_tokens=6,
            overlap_tokens=0,
            semantic_breakpoint=0.5,
        ),
        embedder=_SemanticFixtureEmbedder(),
        token_counter=_word_tokens,
        token_counter_id="fixture-word-count-v1",
    )

    assert document.embedding_model_id == "st:semantic-chunk-fixture"
    assert len(document.parents) == 1  # a page turn alone does not break the PDF text flow
    assert [unit.segment_ids for unit in document.units] == [
        (segments[0].segment_id, segments[1].segment_id),
        (segments[2].segment_id, segments[3].segment_id),
    ]
    assert document.units[0].semantic_break_score is not None
    assert document.units[0].semantic_break_score < 0.5
    assert all(unit.parent_id == document.parents[0].parent_id for unit in document.units)
    assert [locator["page_index"] for locator in document.units[0].locators] == [0, 0]
    assert [locator["page_index"] for locator in document.units[1].locators] == [1, 1]


def test_semantic_strategy_rejects_hashing_baseline() -> None:
    with pytest.raises(SemanticChunkingUnavailable, match="real sentence-transformer"):
        build_retrieval_document(
            _pdf_segments(["one line", "another line"]),
            policy=ChunkingPolicy(
                strategy="semantic",
                min_tokens=1,
                target_tokens=2,
                max_tokens=4,
                overlap_tokens=0,
            ),
            embedder=HashingEmbedder(),
            token_counter=_word_tokens,
        )


def test_source_segment_sidecar_indexes_and_returns_locator_metadata(tmp_path) -> None:
    segments = _pdf_segments(
        [
            "Quest condition: recover the sigil.",
            "Return it to the archivist.",
            "Completion unlocks the north gate.",
        ]
    )
    reference_store = ReferenceStore(tmp_path / "content")
    added = reference_store.add_source_segments(
        title="Quest manuscript",
        segments=segments,
        policy=ChunkingPolicy(
            version="structure-fixture-v1",
            strategy="structure",
            min_tokens=1,
            target_tokens=8,
            max_tokens=20,
            overlap_tokens=0,
        ),
        original_filename="quest.pdf",
        provenance=Provenance.OWN_IP,
        token_counter=_word_tokens,
        token_counter_id="fixture-word-count-v1",
    )
    assert added.source.metadata["artifact_version_id"] == segments[0].artifact_version_id
    sidecar = reference_store.load_retrieval_document(added.source.id)
    assert sidecar is not None
    assert UUID(sidecar.units[0].unit_id)
    assert {
        locator["page_index"] for unit in sidecar.units for locator in unit.locators
    } == {0, 1}

    sqlite_store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        reference_store.sync_index(sqlite_store)
        pack = ReferenceContextBuilder(sqlite_store, embedder=HashingEmbedder()).build(
            "north gate", limit=3
        )
    finally:
        sqlite_store.close()

    assert pack.hits
    hit = pack.hits[0]
    assert hit.metadata["artifact_version_id"] == segments[0].artifact_version_id
    assert hit.metadata["policy_version"] == "structure-fixture-v1"
    locators = json.loads(hit.metadata["locators_json"])
    assert {locator["page_index"] for locator in locators} == {1}

    reference_store.remove(added.source.id)
    assert reference_store.load_retrieval_document(added.source.id) is None
