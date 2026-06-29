"""R5 — does the task-3 lie reach the REAL production action path?

Build a content root with ONLY dialogue_trees (no entities/quests/etc), so the
content_index that VectorRetriever indexes is EMPTY (dialogue_trees are not in
_content_rows). The shared SemanticEmbedder therefore stays lazy through
ProjectContext.open(), and is handed still-lazy to ThemeSweepService. Its first
real embed (in the sweep semantic layer) degrades to hashing — but semantic_used
will report True.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from owcopilot.content.models import ContentBundle, DialogueTree
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import HashingEmbedder
import owcopilot.pipeline.project as projmod


class LazyDegradingEmbedder:
    def __init__(self) -> None:
        self.model_id = "st:bge-m3"
        self.degraded = False
        self._fallback = None

    def embed(self, text):
        return self.embed_many([text])[0]

    def embed_many(self, texts):
        if not texts:
            return []
        if self._fallback is None:
            self._fallback = HashingEmbedder()
            self.degraded = True
            self.model_id = self._fallback.model_id
        return self._fallback.embed_many(texts)


_SHARED = LazyDegradingEmbedder()


def main() -> None:
    # Monkeypatch resolve_embedder so ProjectContext.open() uses our lazy degrader.
    projmod.resolve_embedder = lambda: _SHARED  # type: ignore[assignment]

    tmp = Path(tempfile.mkdtemp(prefix="ow_e2e_"))
    # Try to construct a DialogueTree with whatever fields it requires.
    try:
        tree = DialogueTree(id="t1", title="清晨的市集闲谈",
                            root_node="", nodes={})  # title NOT matching theme
    except Exception:
        # fall back: inspect required fields
        import inspect
        print("DialogueTree fields:", DialogueTree.model_fields.keys())
        raise

    bundle = ContentBundle(dialogue_trees={"t1": tree})
    store = ContentStore(tmp)
    store.save(bundle)

    from owcopilot.app.actions import run_theme_sweep_action

    result = run_theme_sweep_action(tmp, theme="赌博", semantic_threshold=0.3)
    print("shared embedder after action: model_id=%s degraded=%s"
          % (_SHARED.model_id, _SHARED.degraded))
    print("scanned_total:", result["scanned_total"])
    print("semantic_used (reported):", result["semantic_used"])
    md_line = [l for l in result["markdown"].splitlines() if "语义近似" in l]
    print("markdown semantic line:", md_line[0] if md_line else "(none)")
    if result["semantic_used"] and _SHARED.degraded:
        print(">>> PRODUCTION LIE CONFIRMED: semantic_used=True (says bge-m3) but ran on hashing")
    elif not result["semantic_used"]:
        print(">>> honest: semantic_used=False")
    else:
        print(">>> embedder did not degrade")


if __name__ == "__main__":
    main()
