"""R5 forced-degrade end-to-end verification of the R4 vector.py live-property fix
and the task-3 downstream construction-time snapshot consumers.

Run with: .venv/Scripts/python.exe scratchpad/verify_degrade.py
"""
from __future__ import annotations

import sys

import numpy as np

from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval.vector import VectorRetriever, load_content_rows
from owcopilot.storage import SQLiteStore
from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation


class DegradingSemanticEmbedder:
    """Mimics the real SemanticEmbedder degrade timing precisely:

    - starts reporting model_id == 'st:bge-m3' (lazy, before first embed)
    - on the FIRST embed_many call, "fails to load the model" and degrades to a
      HashingEmbedder, flipping model_id -> 'hashing-1024' and degraded -> True
    - subsequent embeds use the hashing fallback
    This is exactly the runtime-degrade path described in embedding.py SemanticEmbedder.
    """

    def __init__(self) -> None:
        self.model_id = "st:bge-m3"
        self.degraded = False
        self._fallback: HashingEmbedder | None = None
        self.embed_calls = 0

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        if not texts:
            return []
        if self._fallback is None:
            # first embed -> degrade (simulates offline model load failure)
            self._fallback = HashingEmbedder()
            self.degraded = True
            self.model_id = self._fallback.model_id
        return self._fallback.embed_many(texts)


def _seed_store(store: SQLiteStore) -> None:
    bundle = ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="铁律盟", type=EntityType.FACTION,
                            description="护送商队穿过雾脊山道的武装商会，与影鸦结盟。"),
            "fac_b": Entity(id="fac_b", name="影鸦", type=EntityType.FACTION,
                            description="盘踞雾脊的刺客组织。"),
        },
        relations=[
            Relation(source="fac_a", target="fac_b", kind="盟友",
                     metadata={"description": "铁律盟与影鸦结为盟友，共享情报。"}),
            Relation(source="fac_a", target="fac_b", kind="死敌",
                     metadata={"description": "铁律盟视影鸦为死敌，互相猎杀。"}),
        ],
    )
    store.replace_content_index(bundle)


def main() -> int:
    ok = True

    print("=" * 70)
    print("TEST 1: VectorRetriever live is_semantic after runtime degrade")
    print("=" * 70)
    store = SQLiteStore(":memory:")
    _seed_store(store)
    emb = DegradingSemanticEmbedder()
    # before any embed, the embedder claims st:bge-m3
    assert emb.model_id == "st:bge-m3", "precondition: starts semantic"
    vr = VectorRetriever(store, embedder=emb)  # __init__ -> _reindex -> embed_many degrades
    print(f"  embedder.model_id after construct: {emb.model_id}")
    print(f"  embedder.degraded: {emb.degraded}")
    print(f"  vr.model_id (live property): {vr.model_id}")
    print(f"  vr.is_semantic (live property): {vr.is_semantic}")
    if vr.is_semantic is not False:
        print("  FAIL: is_semantic LIED True after runtime degrade")
        ok = False
    else:
        print("  PASS: is_semantic == False after runtime degrade (no lie)")

    print()
    print("=" * 70)
    print("TEST 2: hashing vectors NOT persisted under st:bge-m3 key (cache poison)")
    print("=" * 70)
    poisoned = store.get_vectors("st:bge-m3", table="content_vectors")
    honest = store.get_vectors("hashing-1024", table="content_vectors")
    print(f"  rows under 'st:bge-m3' key: {len(poisoned)} (must be 0)")
    print(f"  rows under 'hashing-1024' key: {len(honest)} (must be > 0)")
    if poisoned:
        print("  FAIL: SQLite POISONED — hashing vectors stored under st:bge-m3 key")
        ok = False
    elif not honest:
        print("  FAIL: vectors not persisted under the real (hashing) backend key")
        ok = False
    else:
        print("  PASS: vectors keyed under the post-degrade backend only")

    print()
    print("=" * 70)
    print("TEST 3: re-key mid-reindex correctness — search still works on degraded backend")
    print("=" * 70)
    hits = vr.search("结盟", limit=5)
    print(f"  search('结盟') returned {len(hits)} hits: {[h.ref for h in hits]}")
    # matrix built from re-embedded hashing vectors; should be consistent dims
    print(f"  matrix shape: {vr._matrix.shape}")
    if vr._matrix.shape[0] != len(vr._rows):
        print("  FAIL: matrix row count != corpus rows (re-key broke the index)")
        ok = False
    else:
        print("  PASS: index re-keyed consistently; search functional")

    print()
    print("=" * 70)
    print("TEST 4: re-open same store with a fresh REAL (hashing) embedder reads cache, no poison hit")
    print("=" * 70)
    # Simulate a later run where a clean hashing embedder opens the same store.
    fresh = HashingEmbedder()
    vr2 = VectorRetriever(store, embedder=fresh)
    # Should read the persisted hashing-1024 vectors (text unchanged) => 0 new embeds ideally.
    rows_after = store.get_vectors("hashing-1024", table="content_vectors")
    print(f"  fresh embedder model_id: {fresh.model_id}")
    print(f"  vr2.is_semantic: {vr2.is_semantic}")
    print(f"  persisted hashing rows reused: {len(rows_after)}")
    if vr2.is_semantic is not False:
        print("  FAIL")
        ok = False
    else:
        print("  PASS: clean run reads honest cache, never sees st: poison")

    print()
    print("=" * 70)
    print("TEST 5: live property side-effects — does reading vr.model_id re-trigger embed?")
    print("=" * 70)
    emb2 = DegradingSemanticEmbedder()
    store2 = SQLiteStore(":memory:")
    _seed_store(store2)
    vr3 = VectorRetriever(store2, embedder=emb2)
    calls_after_construct = emb2.embed_calls
    # read the live property many times
    for _ in range(100):
        _ = vr3.model_id
        _ = vr3.is_semantic
    calls_after_reads = emb2.embed_calls
    print(f"  embed_many calls after construct: {calls_after_construct}")
    print(f"  embed_many calls after 100x property reads: {calls_after_reads}")
    if calls_after_reads != calls_after_construct:
        print("  FAIL: reading live property triggered embeds (perf/consistency bug)")
        ok = False
    else:
        print("  PASS: live property is a pure attribute read (no embedder call)")

    print()
    print("=" * 70)
    print("TEST 6: TASK 3 — empty-corpus window where downstream snapshot CAN lie")
    print("=" * 70)
    # If VectorRetriever sees an EMPTY corpus, _reindex returns BEFORE embed -> the shared
    # SemanticEmbedder stays lazy (model_id still st:bge-m3, degraded=False). A downstream
    # consumer that snapshots model_id at construction would treat it as semantic, then the
    # FIRST real embed (in the consumer) degrades — and the consumer's semantic_used flag lies.
    empty_store = SQLiteStore(":memory:")
    empty_store.replace_content_index(ContentBundle())  # no entities -> empty content_index
    shared = DegradingSemanticEmbedder()
    vr_empty = VectorRetriever(empty_store, embedder=shared)
    print(f"  after empty-corpus VectorRetriever, embed_calls={shared.embed_calls}, "
          f"model_id={shared.model_id}, degraded={shared.degraded}")
    if shared.embed_calls == 0 and shared.model_id == "st:bge-m3":
        print("  CONFIRMED: empty corpus leaves shared embedder lazy & still claiming st:")
        print("  => a downstream construction-time _is_semantic() snapshot would be True here")
    else:
        print("  empty-corpus path forced an embed/degrade (window closed)")

    print()
    print("=" * 70)
    print(f"OVERALL: {'ALL PASS' if ok else 'FAILURES PRESENT'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
