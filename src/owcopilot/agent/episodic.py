# 情节记忆的读侧：把过往运行/反思按语义相似度召回进当前 agent 的 prompt。
# 谱系：MemGPT(2310.08560) recall storage「按相似度取回历史消息」+
#       Reflexion(2303.11366) 跨 trial 反思缓冲「过往教训直接进 prompt 的反思槽」。
# 要点：① 只比同一嵌入空间(model_id 一致)的向量；② 嵌入失败 fail-open,只跳过召回/写向量,
#   绝不阻断已完成的 run；③ 离线 $0 走 resolve_embedder() 的 auto→hashing 确定性替身。
"""Episodic-memory READ side: recall semantically-similar past runs and reflections.

The WRITE side of episodic memory already existed (:mod:`owcopilot.agent.history` persists a run's
full transcript; the multi-agent Reflexion retry distils a reflection). This module closes the loop
— the RETRIEVAL half MemGPT (Packer et al., 2023) and Reflexion (Shinn et al., 2023) both require:

* :func:`recall_similar_runs` is MemGPT *recall storage*: embed the current goal, cosine-rank it
  against the stored (goal + outcome) embeddings of past FINISHED runs, and return the top-k as
  :class:`EpisodicHit` s. The CLI folds these into a short "相关历史任务经验" context block.
* :func:`recall_reflections` is the Reflexion *episodic buffer*: the same cosine recall over the
  distilled reflections of prior failed trials, returned as :class:`ReflectionHit` s and fed into
  ``ReActAgent.run(reflections=...)`` — past lessons land directly in the actor's reflection slot.

The write counterparts (:func:`persist_run_embedding`, :func:`persist_reflection`) embed with the
same embedder and record its ``model_id`` alongside the vector: recall only ever compares vectors
produced in the *same* embedding space, because two embedders' vectors are not comparable.

Everything here is fail-open on the EMBEDDING only: an embed failure (e.g. offline on the very
first run before the semantic model is cached) is logged and skipped, so the already-completed run
still persists its transcript and the agent still runs — the recall context is simply absent that
once. Deterministic / $0 in tests via ``OWCOPILOT_EMBEDDER=hashing``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from ..llm.cache import Embedder
    from ..storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)

#: Cap on the outcome text folded into a run's recall embedding / stored summary. Keeps the vector
#: focused on the goal + headline conclusion rather than a long trailing report.
_SUMMARY_MAX_CHARS = 500


@dataclass(frozen=True)
class EpisodicHit:
    """One recalled past run (MemGPT recall storage): its goal, outcome summary and similarity."""

    run_id: str
    goal: str
    summary: str
    similarity: float
    created_at: str


@dataclass(frozen=True)
class ReflectionHit:
    """One recalled reflection (Reflexion episodic buffer): the lesson and its similarity."""

    goal: str
    reflection: str
    source: str
    similarity: float
    created_at: str


# --------------------------------------------------------------------------- write side


def persist_run_embedding(
    store: SQLiteStore,
    embedder: Embedder,
    *,
    run_id: str,
    goal: str,
    final_answer: str,
) -> None:
    """Embed ``goal + summary`` and persist it as ``run_id``'s recall vector (best-effort).

    Called at the persist point AFTER :func:`owcopilot.agent.history.persist_agent_run` has stored
    the run itself, so an embed failure here never blocks the run from being recorded: it is logged
    and skipped, leaving the run present but simply not recallable. The stored ``summary`` is the
    truncated final answer, surfaced verbatim in the recall context block as the run's 结论."""
    summary = final_answer.strip()[:_SUMMARY_MAX_CHARS]
    text = f"{goal} {summary}".strip()
    try:
        vector = embedder.embed(text)
    except Exception as exc:  # noqa: BLE001 — fail-open on the embed only; the run is already stored
        logger.warning(
            "failed to embed agent run %s for episodic recall (%s); the run is stored but will "
            "not be recallable",
            run_id,
            exc,
        )
        return
    try:
        store.save_agent_run_embedding(
            run_id=run_id,
            # Read model_id AFTER embed: a degrading semantic embedder swaps to the hashing id on
            # its first embed, and the vector must be keyed under the id that actually produced it.
            model_id=embedder.model_id,
            goal=goal,
            summary=summary,
            vector=_to_blob(vector),
        )
    except Exception as exc:  # noqa: BLE001 — observability write must not break the primary flow
        logger.warning("failed to persist episodic embedding for run %s: %s", run_id, exc)


def persist_reflection(
    store: SQLiteStore,
    embedder: Embedder,
    *,
    goal: str,
    reflection: str,
    source: str,
) -> None:
    """Persist a distilled ``reflection`` keyed to ``goal`` as a Reflexion cross-trial buffer entry.

    The goal is embedded so the reflection is recallable by semantic similarity, not exact match.
    An embed failure is fail-open: the reflection is still recorded (for audit/history) but without
    a vector, so it will not be recalled — never blocking the retry that produced it."""
    vector_blob: bytes | None
    model_id: str | None
    try:
        vector = embedder.embed(goal.strip())
    except Exception as exc:  # noqa: BLE001 — fail-open: still record the reflection, unranked
        logger.warning(
            "failed to embed goal for reflection recall (%s); the reflection is recorded but "
            "will not be recallable",
            exc,
        )
        vector_blob, model_id = None, None
    else:
        vector_blob, model_id = _to_blob(vector), embedder.model_id
    try:
        store.save_reflection(
            reflection_id=uuid.uuid4().hex,
            goal=goal,
            reflection=reflection,
            source=source,
            vector=vector_blob,
            model_id=model_id,
        )
    except Exception as exc:  # noqa: BLE001 — persistence must not break the Reflexion retry
        logger.warning("failed to persist reflection for goal %r: %s", goal, exc)


# --------------------------------------------------------------------------- read side


def recall_similar_runs(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    k: int = 3,
    min_similarity: float = 0.35,
) -> list[EpisodicHit]:
    """Return the top-``k`` finished past runs most similar to ``goal`` (MemGPT recall storage).

    Embeds ``goal``, cosine-ranks it against every finished run's stored embedding *in the current
    embedder's space* (the store join already excludes non-finished runs and other embedding
    spaces), keeps those clearing ``min_similarity``, and returns the strongest ``k``."""
    query = embedder.embed(goal)
    rows = store.query_agent_run_embeddings(model_id=embedder.model_id)
    return [
        EpisodicHit(
            run_id=str(row["run_id"]),
            goal=str(row["goal"]),
            summary=str(row["summary"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in _rank_by_cosine(query, rows, k=k, min_similarity=min_similarity)
    ]


def recall_reflections(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    k: int = 2,
    min_similarity: float = 0.35,
) -> list[ReflectionHit]:
    """Return the top-``k`` reflections most similar to ``goal`` (Reflexion episodic buffer).

    Same cosine recall as :func:`recall_similar_runs`, over the distilled reflections of prior
    failed trials in the current embedder's space."""
    query = embedder.embed(goal)
    rows = store.query_reflections(model_id=embedder.model_id)
    return [
        ReflectionHit(
            goal=str(row["goal"]),
            reflection=str(row["reflection"]),
            source=str(row["source"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in _rank_by_cosine(query, rows, k=k, min_similarity=min_similarity)
    ]


def format_history_block(hits: list[EpisodicHit]) -> str:
    """Render recalled runs into the prompt context block. Caller guards on non-empty ``hits``."""
    lines = ["## 相关历史任务经验(检索自过往运行,仅供参考)"]
    lines.extend(f"- [{hit.created_at}] 目标:{hit.goal} → 结论:{hit.summary}" for hit in hits)
    return "\n".join(lines)


# --------------------------------------------------------------------------- internals


def _to_blob(vector: list[float]) -> bytes:
    """Serialize an embedding to a float32 blob, matching the retrieval layer's on-disk format."""
    return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


def _rank_by_cosine(
    query: list[float],
    rows: list[dict[str, Any]],
    *,
    k: int,
    min_similarity: float,
) -> list[tuple[dict[str, Any], float]]:
    """Cosine-rank ``rows`` (each carrying a ``vector`` blob) against ``query``; top-``k`` desc.

    Rows whose stored vector has a different dimensionality than the query (a corrupt or
    cross-model row that slipped past the model_id filter) are skipped rather than raising."""
    q = np.ascontiguousarray(query, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if not q_norm:
        return []
    scored: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        if vec.shape != q.shape:
            continue
        vec_norm = float(np.linalg.norm(vec))
        if not vec_norm:
            continue
        similarity = float(np.dot(q, vec)) / (q_norm * vec_norm)
        if similarity >= min_similarity:
            scored.append((row, similarity))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:k]
