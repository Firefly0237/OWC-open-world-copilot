"""Embedder selection: the real semantic embedder, and the auto factory.

The retrieval stack runs against the ``Embedder`` protocol (``llm.cache``). Two backends:

* ``HashingEmbedder`` -- deterministic, dependency-free bag-of-words hash. The test/offline
  baseline; it carries no semantic signal and (by design) drops CJK, so it only exists to keep
  the suite $0 and golden-testable.
* ``SemanticEmbedder`` -- a real multilingual sentence embedding model (default BAAI/bge-m3)
  run locally via sentence-transformers. This is the production path: it makes the "vector"
  leg of the hybrid retriever actually semantic, so paraphrases and synonyms that share no
  words with the canon still retrieve.

``resolve_embedder`` picks the backend. The default is ``auto``: use the semantic model when the
optional ``[semantic]`` dependency is *importable*, otherwise fall back to the hashing stub. The
check is import-only and never loads the model, so process/server startup stays fast — the model
is downloaded (first run, needs network once) and loaded lazily on the first embed. If that load
fails (e.g. offline on the very first run), the semantic embedder degrades to the hashing stub
with a one-time warning instead of crashing retrieval. The choice is overridable by env so CI/tests
pin the deterministic stub while an installed deployment gets real RAG with no code change.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from functools import lru_cache

from ..llm.cache import Embedder, HashingEmbedder

logger = logging.getLogger(__name__)

DEFAULT_SEMANTIC_MODEL = "BAAI/bge-m3"

#: Env knobs. ``OWCOPILOT_EMBEDDER`` in {auto, semantic, hashing}; ``OWCOPILOT_EMBED_MODEL``
#: names the sentence-transformers model when semantic.
_MODE_ENV = "OWCOPILOT_EMBEDDER"
_MODEL_ENV = "OWCOPILOT_EMBED_MODEL"


class SemanticEmbedder:
    """Local multilingual sentence embeddings via sentence-transformers (e.g. bge-m3).

    Vectors are L2-normalised, so a dot product is cosine similarity. The model is loaded lazily
    on first use and cached per process; nothing here touches the network at import. If the model
    cannot be loaded at first use (e.g. no network on the very first run), the embedder degrades to
    the deterministic hashing stub so retrieval keeps working on BM25 + graph, not crashing."""

    def __init__(self, model_name: str = DEFAULT_SEMANTIC_MODEL) -> None:
        self.model_name = model_name
        self.model_id = f"st:{model_name}"
        self._model: object | None = None
        self._fallback: HashingEmbedder | None = None

    def _ensure_model(self) -> object:
        if self._model is None:
            self._model = _load_model(self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._fallback is not None:
            return self._fallback.embed_many(texts)
        try:
            model = self._ensure_model()
        except Exception as exc:  # noqa: BLE001 - degrade rather than break retrieval
            logger.warning(
                "semantic embedder %s could not load (%s); falling back to the hashing embedder "
                "for this process. Install the model with network access once to enable semantic "
                "retrieval.",
                self.model_name,
                exc,
            )
            self._fallback = HashingEmbedder()
            return self._fallback.embed_many(texts)
        # normalize so cosine == dot; convert_to_numpy keeps memory flat for large batches.
        vectors = model.encode(  # type: ignore[attr-defined]
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return [[float(value) for value in row] for row in vectors]


@lru_cache(maxsize=4)
def _load_model(model_name: str) -> object:
    # Quiet the Hub's "unauthenticated requests" notice and progress bars: we pull a public model
    # and need no token, so it is just noise on a first-run download. Set verbosity via env BEFORE
    # huggingface_hub is imported (it reads HF_HUB_VERBOSITY at import), and filter the warning as a
    # belt-and-suspenders in case it is emitted through the warnings module.
    import warnings

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", message=".*unauthenticated requests to the HF Hub.*")
    from sentence_transformers import SentenceTransformer  # heavy, optional, lazy

    return SentenceTransformer(model_name)


def semantic_installed() -> bool:
    """True when the optional ``[semantic]`` dependency is importable (no model load/download)."""
    return importlib.util.find_spec("sentence_transformers") is not None


def semantic_available(model_name: str = DEFAULT_SEMANTIC_MODEL) -> bool:
    """True when the optional dependency imports and the model can be constructed (loads it)."""
    try:
        _load_model(model_name)
        return True
    except Exception:
        return False


def resolve_embedder() -> Embedder:
    """Return the embedder per env: ``auto`` (default) uses semantic when installed.

    ``auto`` only checks that the dependency is importable — it does NOT load the model — so this
    is safe to call at startup without a multi-second cold start or a network round-trip.
    """
    mode = os.getenv(_MODE_ENV, "auto").strip().lower()
    model = os.getenv(_MODEL_ENV, DEFAULT_SEMANTIC_MODEL).strip() or DEFAULT_SEMANTIC_MODEL
    if mode == "hashing":
        return HashingEmbedder()
    if mode == "semantic":
        # Explicit opt-in: fail loud if the model can't load rather than silently degrading.
        embedder = SemanticEmbedder(model)
        embedder._ensure_model()
        return embedder
    # auto: lazy — return the semantic embedder when the dep is present (it loads on first embed,
    # degrading to hashing if that load fails), else the deterministic stub.
    if semantic_installed():
        return SemanticEmbedder(model)
    return HashingEmbedder()
