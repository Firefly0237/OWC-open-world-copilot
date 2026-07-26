# 情节记忆的读侧 + 生命周期：把过往运行/反思按「三因子」召回进当前 agent 的 prompt,并在写入时
#   做去重 / 超容量软删除,让记忆随时间衰减、不无限膨胀。
# 谱系：MemGPT(2310.08560) recall storage「按相似度取回历史消息」+
#       Reflexion(2303.11366) 跨 trial 反思缓冲「过往教训直接进 prompt 的反思槽」+
#       Generative Agents(2304.03442) 检索打分「relevance + recency + importance」。
# 三因子(召回排序)= relevance 语义相似 × recency 时间衰减 × importance 重要度,三者相乘:任一因子
#   趋零就把该记忆压到后面(旧且不相关的经验自然沉底)。retention(容量软删除的保留分,与查询无关)
#   = recency × importance —— relevance 依赖当前 query,只用于召回;recency/importance 是记忆内禀属性,
#   用于「留谁 / 删谁」。importance 现按现有数据取代理值:run 用 step_count(完成的 run 步数越多=经验
#   越实),reflection 无逐条重要度信号→取常量(诚实降级,见模块尾 WEAKNESSES)。
# 要点：① 只比同一嵌入空间(model_id 一致)的向量；② 嵌入失败 fail-open,只跳过召回/写向量,
#   绝不阻断已完成的 run;容量/去重同样 best-effort,失败只记日志；③ 离线 $0 走 resolve_embedder()
#   的 auto→hashing 确定性替身；④ 软删除(deleted_at 墓碑)而非物理删,误删可 restore 恢复。
"""Episodic-memory READ side + lifecycle: three-factor recall, dedup, capacity soft-delete, decay.

The WRITE side of episodic memory already existed (:mod:`owcopilot.agent.history` persists a run's
full transcript; the multi-agent Reflexion retry distils a reflection). This module closes the loop
— the RETRIEVAL half MemGPT (Packer et al., 2023) and Reflexion (Shinn et al., 2023) require —
*and* the lifecycle that keeps the memory store useful as it grows (Generative Agents,
Park et al., 2023):

* :func:`recall_similar_runs` is MemGPT *recall storage*: embed the current goal, rank past FINISHED
  runs by ``relevance × recency × importance`` (see :func:`_rank_and_dedup`), drop near-duplicates,
  and return the top-k as :class:`EpisodicHit` s. The CLI folds these into a history context block.
* :func:`recall_reflections` is the Reflexion *episodic buffer*: the same three-factor recall over
  the distilled reflections of prior failed trials, fed into ``ReActAgent.run(reflections=...)`` —
  past lessons land directly in the actor's reflection slot.

Lifecycle, wired into the write path so it is genuinely in effect (not a scoring function nobody
calls):

* **Three factors** — recall ranks by ``relevance × recency × importance`` (a multiplicative model:
  an old *or* unimportant *or* unrelated memory is pushed down). ``EpisodicHit.similarity`` still
  exposes the raw cosine so callers see the semantic match, not the blended score.
* **Dedup** — near-duplicate hits (mutual cosine ≥ :data:`_DEDUP_COSINE_THRESHOLD`) collapse to the
  single best-scored one, so one experience recorded twice cannot crowd the recall slot / capacity.
* **Capacity soft-delete** — after each write, if a space holds more than :data:`_EPISODIC_CAPACITY`
  live memories, the lowest ``recency × importance`` ones are *soft-deleted* (tombstoned via
  ``deleted_at``), recoverable with ``store.restore_*``; recall filters tombstones out.
* **Recency decay** — a memory's recency factor halves every :data:`_RECENCY_HALF_LIFE_HOURS`, so an
  older memory ranks (and is retained) below an otherwise-equal newer one.

The write counterparts (:func:`persist_run_embedding`, :func:`persist_reflection`) embed with the
same embedder and record its ``model_id`` alongside the vector: recall only ever compares vectors
produced in the *same* embedding space, because two embedders' vectors are not comparable.

Everything here is fail-open on the EMBEDDING only: an embed failure (e.g. offline on the very
first run before the semantic model is cached) is logged and skipped, so the already-completed run
still persists its transcript and the agent still runs — the recall context is simply absent that
once. Capacity enforcement is likewise best-effort (a failure logs and leaves the store intact).
Deterministic / $0 in tests via ``OWCOPILOT_EMBEDDER=hashing``.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import EPISODIC_RECALL_MIN_SIMILARITY

if TYPE_CHECKING:
    from ..llm.cache import Embedder
    from ..storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)

#: Cap on the outcome text folded into a run's recall embedding / stored summary. Keeps the vector
#: focused on the goal + headline conclusion rather than a long trailing report.
_SUMMARY_MAX_CHARS = 500

#: Recency half-life. A memory's recency factor is ``0.5 ** (age_hours / half_life)`` — it halves
#: every week, so a day-old and an hour-old memory are near-indistinguishable (gentle for a
#: long-lived narrative project) while a months-old one is heavily discounted.
_RECENCY_HALF_LIFE_HOURS = 168.0

#: Saturation scale for a run's step-count importance proxy. ``1 - exp(-steps / scale)`` reaches
#: ~0.63 at ``scale`` steps and plateaus toward 1.0 — a finished run that took more reasoning steps
#: is treated as a more substantive episode. Proxy for the absent LLM-scored poignancy (see WEAK).
_IMPORTANCE_STEP_SCALE = 6.0

#: Floor for any single factor, so one zero-ish factor cannot annihilate the product outright
#: (a still-relevant but old-and-cheap memory keeps a small positive score instead of vanishing).
_MIN_FACTOR = 0.05

#: A reflection carries no per-item importance signal, so its importance factor is a constant. It is
#: an already-distilled lesson; recency + relevance do the ranking. (Honest degradation — see WEAK.)
_REFLECTION_IMPORTANCE = 1.0

#: Mutual-cosine cutoff above which two recall candidates are treated as the *same* experience and
#: collapsed to the best-scored one. 0.95 collapses true duplicates (cosine 1.0) while keeping
#: genuinely distinct-but-similar episodes (empirically ~0.86 apart under the hashing embedder).
_DEDUP_COSINE_THRESHOLD = 0.95

#: Per-embedding-space soft-delete capacity. Beyond this many live memories, the lowest-retention
#: ones are tombstoned on write. Sized so ordinary use never evicts; it bounds unbounded growth on a
#: long-lived project. Overridable per call (tests drive eviction with a small capacity).
_EPISODIC_CAPACITY = 512


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
    capacity: int = _EPISODIC_CAPACITY,
) -> None:
    """Embed ``goal + summary`` and persist it as ``run_id``'s recall vector (best-effort).

    Called at the persist point AFTER :func:`owcopilot.agent.history.persist_agent_run` has stored
    the run itself, so an embed failure here never blocks the run from being recorded: it is logged
    and skipped, leaving the run present but simply not recallable. The stored ``summary`` is the
    truncated final answer, surfaced verbatim in the recall context block as the run's 结论.

    After a successful write the space's capacity is enforced (:func:`_enforce_run_capacity`): if it
    now holds more than ``capacity`` live embeddings, the lowest ``recency × importance`` ones are
    soft-deleted so the store cannot grow without bound. ``capacity`` is exposed for tests."""
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
    # Read model_id AFTER embed: a degrading semantic embedder swaps to the hashing id on its first
    # embed, and the vector must be keyed under the id that actually produced it.
    model_id = embedder.model_id
    try:
        store.save_agent_run_embedding(
            run_id=run_id,
            model_id=model_id,
            goal=goal,
            summary=summary,
            vector=_to_blob(vector),
        )
    except Exception as exc:  # noqa: BLE001 — observability write must not break the primary flow
        logger.warning("failed to persist episodic embedding for run %s: %s", run_id, exc)
        return
    _enforce_run_capacity(store, model_id=model_id, capacity=capacity)


def persist_reflection(
    store: SQLiteStore,
    embedder: Embedder,
    *,
    goal: str,
    reflection: str,
    source: str,
    capacity: int = _EPISODIC_CAPACITY,
) -> None:
    """Persist a distilled ``reflection`` keyed to ``goal`` as a Reflexion cross-trial buffer entry.

    The goal is embedded so the reflection is recallable by semantic similarity, not exact match.
    An embed failure is fail-open: the reflection is still recorded (for audit/history) but without
    a vector, so it will not be recalled — never blocking the retry that produced it.

    On a successful embedded write the reflection space's capacity is enforced (lowest-recency
    entries soft-deleted beyond ``capacity``); a reflection without a vector carries no ``model_id``
    to scope the sweep to, so capacity is skipped for it. ``capacity`` is exposed for tests."""
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
        return
    if model_id is not None:
        _enforce_reflection_capacity(store, model_id=model_id, capacity=capacity)


# --------------------------------------------------------------------------- read side


def recall_similar_runs(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    k: int = 3,
    min_similarity: float = EPISODIC_RECALL_MIN_SIMILARITY,
) -> list[EpisodicHit]:
    """Return the top-``k`` finished past runs most similar to ``goal`` (MemGPT recall storage).

    Embeds ``goal``, then ranks every finished, live run embedding *in the current embedder's space*
    (the store join already excludes non-finished / soft-deleted rows and other embedding spaces) by
    ``relevance × recency × importance``, drops near-duplicates, and returns the strongest ``k``.
    ``EpisodicHit.similarity`` is the raw cosine (the semantic match), not the blended score."""
    query = embedder.embed(goal)
    rows = store.query_agent_run_embeddings(model_id=embedder.model_id)
    ranked = _rank_and_dedup(
        query,
        rows,
        k=k,
        min_similarity=min_similarity,
        now=datetime.now(UTC),
        importance_of=lambda row: _run_importance(int(row["step_count"])),
    )
    return [
        EpisodicHit(
            run_id=str(row["run_id"]),
            goal=str(row["goal"]),
            summary=str(row["summary"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in ranked
    ]


def recall_reflections(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    k: int = 2,
    min_similarity: float = EPISODIC_RECALL_MIN_SIMILARITY,
) -> list[ReflectionHit]:
    """Return the top-``k`` reflections most similar to ``goal`` (Reflexion episodic buffer).

    Same three-factor recall as :func:`recall_similar_runs`, over the distilled reflections of prior
    failed trials in the current embedder's space. A reflection carries no per-item importance
    signal, so its importance factor is the constant :data:`_REFLECTION_IMPORTANCE`; relevance and
    recency do the ranking."""
    query = embedder.embed(goal)
    rows = store.query_reflections(model_id=embedder.model_id)
    ranked = _rank_and_dedup(
        query,
        rows,
        k=k,
        min_similarity=min_similarity,
        now=datetime.now(UTC),
        importance_of=lambda _row: _REFLECTION_IMPORTANCE,
    )
    return [
        ReflectionHit(
            goal=str(row["goal"]),
            reflection=str(row["reflection"]),
            source=str(row["source"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in ranked
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


def _run_importance(step_count: int) -> float:
    """Importance proxy for a run: a saturating function of its step count.

    A *finished* run that took more reasoning steps engaged the problem more deeply, so it is a more
    substantive episode to recall. This is a proxy for the absent LLM-scored poignancy (Generative
    Agents) — the honest degradation is documented in the module WEAKNESSES note. Floored at
    :data:`_MIN_FACTOR` so a one-step run still contributes a small positive importance."""
    return max(_MIN_FACTOR, 1.0 - math.exp(-max(0, step_count) / _IMPORTANCE_STEP_SCALE))


def _recency(created_at: str, now: datetime) -> float:
    """Recency factor ``0.5 ** (age_hours / half_life)`` — halves each ``_RECENCY_HALF_LIFE_HOURS``.

    Fail-open on an unparseable / future timestamp: an unparseable ``created_at`` is treated as
    fresh (factor 1.0) rather than crashing recall, and a future timestamp clamps age to 0."""
    ts = _parse_iso(created_at)
    if ts is None:
        return 1.0
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return max(_MIN_FACTOR, 0.5 ** (age_hours / _RECENCY_HALF_LIFE_HOURS))


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO / SQLite timestamp to an aware UTC datetime, or ``None`` if unparseable.

    Naive timestamps (e.g. a SQLite ``CURRENT_TIMESTAMP`` default) are assumed UTC so the age
    subtraction against an aware ``now`` never raises."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _rank_and_dedup(
    query: list[float],
    rows: list[dict[str, Any]],
    *,
    k: int,
    min_similarity: float,
    now: datetime,
    importance_of: Callable[[dict[str, Any]], float],
) -> list[tuple[dict[str, Any], float]]:
    """Three-factor rank + dedup of ``rows`` (each carrying a ``vector`` blob) against ``query``.

    Each candidate clearing the ``min_similarity`` *relevance* gate is scored
    ``relevance × recency × importance`` and the set ordered by that blended score. Near-duplicate
    candidates (mutual cosine ≥ :data:`_DEDUP_COSINE_THRESHOLD`) collapse to the highest-scored one,
    so one experience recorded twice occupies a single recall slot. Returns the top-``k`` survivors
    as ``(row, cosine)`` — the cosine (not the blended score) is what the caller exposes as
    ``similarity``. Rows whose stored vector dimensionality differs from the query (a corrupt or
    cross-model row that slipped past the model_id filter) are skipped rather than raising."""
    q = np.ascontiguousarray(query, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if not q_norm:
        return []
    # (row, cosine, blended_score, unit_vector) for every candidate above the relevance gate.
    scored: list[tuple[dict[str, Any], float, float, np.ndarray]] = []
    for row in rows:
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        if vec.shape != q.shape:
            continue
        vec_norm = float(np.linalg.norm(vec))
        if not vec_norm:
            continue
        cosine = float(np.dot(q, vec)) / (q_norm * vec_norm)
        if cosine < min_similarity:
            continue
        blended = cosine * _recency(str(row["created_at"]), now) * importance_of(row)
        scored.append((row, cosine, blended, vec / vec_norm))
    scored.sort(key=lambda item: item[2], reverse=True)
    kept: list[tuple[dict[str, Any], float, np.ndarray]] = []
    for row, cosine, _blended, unit in scored:
        # A candidate that near-duplicates one already kept (which outranks it) is dropped.
        if any(
            float(np.dot(unit, kept_unit)) >= _DEDUP_COSINE_THRESHOLD for _, _, kept_unit in kept
        ):
            continue
        kept.append((row, cosine, unit))
        if len(kept) >= k:
            break
    return [(row, cosine) for row, cosine, _ in kept]


# --------------------------------------------------------------------------- capacity lifecycle


def _enforce_run_capacity(store: SQLiteStore, *, model_id: str, capacity: int) -> None:
    """Soft-delete the lowest-retention run embeddings in ``model_id`` beyond ``capacity``.

    Retention (query-independent) = ``recency × importance``; the lowest ``count - capacity`` live
    embeddings are tombstoned so the space stays bounded. Best-effort: any failure logs and leaves
    the store intact — capacity housekeeping must never break the run that just wrote a memory."""
    try:
        rows = store.list_run_embedding_retention(model_id=model_id)
        if len(rows) <= capacity:
            return
        now = datetime.now(UTC)
        ordered = sorted(
            rows,
            key=lambda row: _recency(str(row["created_at"]), now)
            * _run_importance(int(row["step_count"])),
        )
        evict = [str(row["run_id"]) for row in ordered[: len(rows) - capacity]]
        store.soft_delete_agent_run_embeddings(evict)
    except Exception as exc:  # noqa: BLE001 — capacity sweep is best-effort; never break the flow
        logger.warning("episodic run-capacity sweep failed for space %s: %s", model_id, exc)


def _enforce_reflection_capacity(store: SQLiteStore, *, model_id: str, capacity: int) -> None:
    """Soft-delete the lowest-retention reflections in ``model_id`` beyond ``capacity``.

    Reflections carry constant importance, so retention reduces to recency: the oldest live entries
    beyond ``capacity`` are tombstoned. Best-effort, same as :func:`_enforce_run_capacity`."""
    try:
        rows = store.list_reflection_retention(model_id=model_id)
        if len(rows) <= capacity:
            return
        now = datetime.now(UTC)
        ordered = sorted(
            rows,
            key=lambda row: _recency(str(row["created_at"]), now) * _REFLECTION_IMPORTANCE,
        )
        evict = [str(row["id"]) for row in ordered[: len(rows) - capacity]]
        store.soft_delete_reflections(evict)
    except Exception as exc:  # noqa: BLE001 — capacity sweep is best-effort; never break the flow
        logger.warning("episodic reflection-capacity sweep failed for space %s: %s", model_id, exc)


# WEAKNESSES (honest degradations of the three-factor lifecycle):
#   * importance for RUNS is a *proxy* (saturating step_count), not the LLM-scored poignancy of
#     Generative Agents. A short but pivotal run is under-weighted; a long floundering-then-finished
#     run is over-weighted. It is the strongest signal already on the run row (no extra LLM call on
#     the fail-open write path). A future poignancy score can replace `_run_importance` unchanged.
#   * importance for REFLECTIONS is a constant — there is no per-item signal to rank one distilled
#     lesson above another — so reflection recall ranks on relevance × recency only.
#   * capacity bounds the *stored volume* per embedding space (all live embeddings, finished or
#     not), not only the recall-eligible finished set; an unfinished high-step embedding can occupy
#     a slot it is never recalled into. Acceptable: it still bounds growth, unfinished runs are few.
#   * recovery is per-id (`store.restore_agent_run_embedding` / `restore_reflection`): a deliberate,
#     targeted un-tombstone; there is no bulk "undo last sweep", since eviction is by design.
