"""Real semantic retrieval -- skipped unless the optional [semantic] model is available.

These prove the dense leg bridges a vocabulary/language gap that the lexical stub cannot:
a query sharing zero words with the canon (cross-lingual) still retrieves the right entity,
and the hybrid reranker keeps that semantic hit on top instead of demoting it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sentence_transformers")

from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.llm.cache import HashingEmbedder  # noqa: E402
from owcopilot.pipeline.project import ProjectContext  # noqa: E402
from owcopilot.retrieval.embedding import (  # noqa: E402
    DEFAULT_SEMANTIC_MODEL,
    SemanticEmbedder,
    semantic_available,
)

pytestmark = pytest.mark.skipif(
    not semantic_available(DEFAULT_SEMANTIC_MODEL),
    reason="semantic embedding model not available",
)


def _world() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_tide_lord": Entity(
                id="npc_tide_lord",
                name="深海领主",
                type=EntityType.NPC,
                description="统治潮汐沉没之城的古老存在，掌控海流与风暴。",
            ),
            "npc_smith": Entity(
                id="npc_smith",
                name="铁砧加罗什",
                type=EntityType.NPC,
                description="在边境村庄锻造兵器、修理盔甲的矮人工匠。",
            ),
        }
    )


def _build(tmp_path, embedder):
    root = tmp_path / "w"
    ContentStore(root).save(_world())
    return ProjectContext.open(root, sqlite_path=tmp_path / "w.sqlite", embedder=embedder)


def test_semantic_retrieves_cross_lingual_match_lexical_misses(tmp_path) -> None:
    query = "who rules the sunken tidal city"  # zero shared characters with the canon

    lexical = _build(tmp_path / "lex", HashingEmbedder())
    try:
        assert "entity:npc_tide_lord" not in lexical.context_builder.build(query).refs
    finally:
        lexical.close()

    semantic = _build(tmp_path / "sem", SemanticEmbedder(DEFAULT_SEMANTIC_MODEL))
    try:
        pack = semantic.context_builder.build(query, budget_tokens=10_000)
        assert pack.refs[0] == "entity:npc_tide_lord"
    finally:
        semantic.close()


def test_semantic_paraphrase_retrieval(tmp_path) -> None:
    semantic = _build(tmp_path, SemanticEmbedder(DEFAULT_SEMANTIC_MODEL))
    try:
        pack = semantic.context_builder.build("I need someone to mend my broken armor")
        assert pack.refs[0] == "entity:npc_smith"
    finally:
        semantic.close()
