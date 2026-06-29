"""R5 repro: sweep.py work-order falsely advertises 'bge-m3 enabled' after a mid-run degrade.

A lazy SemanticEmbedder reports model_id='st:...' at construction; ThemeSweeper snapshots
_is_semantic at __init__ (keeps the embedder), then runs sweep() which embeds. If the embed
degrades to hashing, report.semantic_used stays True and the human-facing work order prints
"语义近似（向量）：已启用（bge-m3 ...）" — a silent-downgrade-as-success.
"""
from owcopilot.retrieval.embedding import SemanticEmbedder
from owcopilot.assist.sweep import ThemeSweepService, render_sweep_markdown, _is_semantic
from owcopilot.content.models import ContentBundle, Entity, EntityType


def main() -> None:
    emb = SemanticEmbedder(model_name="this/model-does-not-exist-xyz")
    print("at construction: model_id =", emb.model_id, "| _is_semantic =", _is_semantic(emb))

    bundle = ContentBundle()
    # Entities whose text does NOT contain the theme word, so they land in `pending` and get
    # embedded by _semantic_scores — which triggers the lazy load + degrade mid-sweep.
    bundle.entities["a"] = Entity(id="a", name="北境工会", type=EntityType.FACTION, description="北境工会守护古道与商路。")
    bundle.entities["b"] = Entity(id="b", name="守军", type=EntityType.NPC, description="守军在山口巡逻并护送商队。")

    sweeper = ThemeSweepService(bundle=bundle, embedder=emb)
    print("after ctor: sweeper.embedder is None? ->", sweeper.embedder is None)

    # theme word appears in NO entity text -> both go to pending -> _semantic_scores embeds them
    report = sweeper.sweep("背叛阴谋", semantic_threshold=0.99)
    print("AFTER sweep():")
    print("  embedder.model_id    =", emb.model_id, "(degraded=", getattr(emb, "degraded", "?"), ")")
    print("  report.semantic_used =", report.semantic_used, "  <-- THE CLAIM")
    print("  _is_semantic(emb) now =", _is_semantic(emb), " <-- ground truth")

    wo = render_sweep_markdown(report)
    # find the 语义近似 line
    for line in wo.splitlines():
        if "语义近似" in line:
            print("  WORK ORDER LINE:", line.strip())
    if report.semantic_used and not _is_semantic(emb):
        print(">>> LIE CONFIRMED: work order claims bge-m3 enabled but backend degraded to hashing")


if __name__ == "__main__":
    main()
