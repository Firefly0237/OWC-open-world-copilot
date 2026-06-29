"""R5 task-3 verification: do contradiction.py / sweep.py LIE about semantic_used
when handed a still-lazy SemanticEmbedder that degrades on first embed?

The realistic trigger: the shared project embedder has NOT yet been forced to embed
(e.g. VectorRetriever saw an empty corpus, OR the consumer is constructed before the
vector index touches the model), so model_id is still 'st:bge-m3' at construction time.
The consumer snapshots _is_semantic() == True, keeps the embedder, then its own first
embed_many degrades to hashing — but semantic_used stays True.
"""
from __future__ import annotations

from owcopilot.llm.cache import HashingEmbedder
from owcopilot.content.models import ContentBundle, Entity, EntityType, Relation
from owcopilot.assist.sweep import ThemeSweepService
from owcopilot.assist.contradiction import ContradictionDetector


class LazyDegradingEmbedder:
    """Exactly the SemanticEmbedder runtime profile: lazy 'st:bge-m3' until first embed,
    then degrades to hashing on first embed_many."""

    def __init__(self) -> None:
        self.model_id = "st:bge-m3"
        self.degraded = False
        self._fallback: HashingEmbedder | None = None

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts):
        if not texts:
            return []
        if self._fallback is None:
            self._fallback = HashingEmbedder()
            self.degraded = True
            self.model_id = self._fallback.model_id
        return self._fallback.embed_many(texts)


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "fac_a": Entity(id="fac_a", name="铁律盟", type=EntityType.FACTION,
                            description="护送商队穿过雾脊山道的武装商会。"),
            "fac_b": Entity(id="fac_b", name="影鸦", type=EntityType.FACTION,
                            description="盘踞雾脊的刺客组织，与铁律盟有往来。"),
        },
        relations=[
            Relation(source="fac_a", target="fac_b", kind="盟友",
                     metadata={"description": "铁律盟与影鸦结为盟友。"}),
            Relation(source="fac_a", target="fac_b", kind="死敌",
                     metadata={"description": "铁律盟视影鸦为死敌。"}),
        ],
    )


print("=" * 70)
print("TASK 3a: ThemeSweepService.semantic_used with a lazy-degrading embedder")
print("=" * 70)
emb = LazyDegradingEmbedder()
# construct BEFORE any embed has happened (model_id still st:bge-m3)
assert emb.model_id == "st:bge-m3"
svc = ThemeSweepService(bundle=_bundle(), embedder=emb)
print(f"  embedder.model_id at construct: st:bge-m3 -> svc kept embedder? {svc.embedder is not None}")
report = svc.sweep("赌博", semantic_threshold=0.5)  # runs semantic layer -> first embed -> degrade
print(f"  AFTER sweep: embedder.model_id={emb.model_id}, degraded={emb.degraded}")
print(f"  report.semantic_used = {report.semantic_used}")
print(f"  report markdown semantic line would say 'bge-m3'? {report.semantic_used}")
if report.semantic_used and emb.degraded:
    print("  >>> BUG CONFIRMED: semantic_used=True but embedder actually ran on HASHING")
else:
    print("  no lie")

print()
print("=" * 70)
print("TASK 3b: ContradictionDetector.semantic_used with a lazy-degrading embedder")
print("=" * 70)
emb2 = LazyDegradingEmbedder()
assert emb2.model_id == "st:bge-m3"
det = ContradictionDetector(bundle=_bundle(), embedder=emb2)
print(f"  detector kept embedder? {det.embedder is not None}")
rep2 = det.detect(use_llm=False, semantic_threshold=0.6)
print(f"  AFTER detect: embedder.model_id={emb2.model_id}, degraded={emb2.degraded}")
print(f"  report.semantic_used = {rep2.semantic_used}")
if rep2.semantic_used and emb2.degraded:
    print("  >>> BUG CONFIRMED: semantic_used=True but embedder actually ran on HASHING")
else:
    print("  no lie")

print()
print("=" * 70)
print("CONTRAST: if the embedder had ALREADY degraded before construction (normal flow)")
print("=" * 70)
emb3 = LazyDegradingEmbedder()
emb3.embed_many(["force degrade now"])  # simulate VectorRetriever already forced it
print(f"  pre-degraded embedder model_id: {emb3.model_id}")
svc3 = ThemeSweepService(bundle=_bundle(), embedder=emb3)
rep3 = svc3.sweep("赌博", semantic_threshold=0.5)
print(f"  svc kept embedder? {svc3.embedder is not None} (expect False)")
print(f"  report.semantic_used = {rep3.semantic_used} (expect False — honest)")
