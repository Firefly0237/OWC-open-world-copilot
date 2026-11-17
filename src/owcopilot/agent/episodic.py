# 情节记忆的读侧 + 生命周期：把过往运行/反思按「三因子」召回进当前 agent 的 prompt,并在写入时
#   做去重 / 超容量软删除,让记忆随时间衰减、不无限膨胀。
# 机制：历史消息召回 recall storage「按相似度取回历史消息」+
#       跨轮反馈记忆 跨 trial 反思缓冲「过往教训直接进 prompt 的反思槽」+
#       三因子检索 检索打分「relevance + recency + importance」。
# 三因子(召回排序)= relevance 语义相似 × recency 时间衰减 × importance 重要度,三者相乘:任一因子
#   趋零就把该记忆压到后面(旧且不相关的经验自然沉底)。retention(容量软删除的保留分,与查询无关)
#   = recency × importance —— relevance 依赖当前 query,只用于召回;recency/importance 是记忆内禀属性,
#   用于「留谁 / 删谁」。importance 的取值口径(D3)= 这次 run 拿到的**确定性审计裁决**有多大(审计报出
#   的 open error 数 / 影子校验说能消掉的错误数),不是它跑了多少步——**确定性审计=可验证奖励**这条主线
#   在记忆维度上的延伸:同一套规则引擎既在 run 内给 agent 可核验的奖励,也在 run 之后决定哪段经验值得
#   记住。reflection 无逐条重要度信号→取常量(诚实降级,见模块尾 WEAKNESSES)。
# 要点：① 只比同一嵌入空间(model_id 一致)的向量；② 嵌入失败 fail-open,只跳过召回/写向量,
#   绝不阻断已完成的 run;容量/去重同样 best-effort,失败只记日志；③ 离线 $0 走 resolve_embedder()
#   的 auto→hashing 确定性替身；④ 软删除(deleted_at 墓碑)而非物理删,误删可 restore 恢复。
# 与官方 BaseStore 的关系(为什么没走 create_agent(store=))：官方形参确实存在(langchain 1.3.14 的
#   create_agent 签名有 store: BaseStore | None = None,节点侧从 Runtime.store 取),我们知道它、
#   也不是在重造它。BaseStore 的语义面是 namespace(tuple)+key 的 KV:put/get/delete/search/
#   list_namespaces,search 只按 namespace 前缀 + 可选语义 query + filter 返回单一 score 排序的条目
#   (条目带 created_at/updated_at 但不参与排序),TTL 是可选能力(supports_ttl 默认 False)且语义是
#   到期删除。本模块要的四件——三因子排序、retention=recency×importance 的容量软删除、墓碑+逐条
#   restore、只比同一 model_id 嵌入空间——都落在那个语义面之外,不是它的实现细节。所以这里是在
#   BaseStore 语义之外另建一层,补充而非替代:记忆行与 audit/telemetry 同一个 SQLiteStore,不需要
#   第二套持久化;真要接官方宿主,正确做法是写一个 BaseStore 适配器把 put/get/search 映射到这些表,
#   而不是把三因子和墓碑硬塞进 BaseStore 的接口。
"""Episodic-memory READ side + lifecycle: three-factor recall, dedup, capacity soft-delete, decay.

The WRITE side of episodic memory already existed (:mod:`owcopilot.agent.history` persists a run's
full transcript; the retry driver stores correction notes). This module retrieves those records
and maintains their retention, deduplication and scope:

* :func:`recall_similar_runs` is historical-message recall *recall storage*: embed the current
goal, rank past FINISHED
  runs by ``relevance × recency × importance`` (see :func:`_rank_and_dedup`), drop near-duplicates,
  and return the top-k as :class:`EpisodicHit` s. The CLI folds these into a history context block.
* :func:`recall_reflections` is the feedback-memory *episodic buffer*: the same three-factor
recall over
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
* **Importance = the deterministic audit's verdict (D3)** — a run's importance factor is the size
  of the *checkable* result it obtained (:func:`run_audit_signal`): how many deterministic-checker
  verdicts it got, plus how many findings / shadow-validated repairs those verdicts carried. This
  is the project's "deterministic audit as a **verifiable reward**" thesis extended into the memory
  dimension — the same rule engine that gives the agent a machine-checkable reward *inside* a run
  decides which runs are worth remembering *after* it, so importance is recomputable by anyone from
  the transcript instead of being an LLM's or an author's opinion. It replaces the pre-D3 proxy
  (a saturating function of ``step_count``), which measured effort rather than outcome and
  therefore under-weighted a short pivotal run and over-weighted a long floundering one.
* **Recency decay (D1)** — a memory's recency factor halves every :data:`_RECENCY_HALF_LIFE_HOURS`,
  decaying from its ``last_accessed_at`` — recency is measured as elapsed time since
  the memory's most recent *access*, not its creation — so a memory that keeps getting recalled
  stays ranked ahead of an equally-old one nobody ever asked for again. A never-recalled memory has
  no ``last_accessed_at`` yet and falls back to ``created_at`` (see :func:`_recency_basis`); a
  successful recall writes the touch back (:func:`_touch_recalled_runs` /
:func:`_touch_recalled_reflections`), best-effort, so a failed touch never breaks the recall that
  triggered it.
* **Namespace / scope (D2)** — every write/recall is confined to the store's current ``world_id``
  (see the "Namespace / scope" note below for why that value, not a checkpointer ``thread_id``, is
  the LangGraph-style namespace here). Two projects/worlds sharing one ``runtime.sqlite`` file can
  never recall or evict each other's memories.

The write counterparts (:func:`persist_run_embedding`, :func:`persist_reflection`) embed with the
same embedder and record its ``model_id`` alongside the vector: recall only ever compares vectors
produced in the *same* embedding space, because two embedders' vectors are not comparable.

Relationship to LangGraph's ``BaseStore`` (why this is not ``create_agent(store=...)``):
    The official seam exists and we know it — ``create_agent`` (langchain 1.3.14) takes
    ``store: BaseStore | None = None`` and nodes read it off ``Runtime.store``. What it offers is a
    namespaced key-value surface: ``put`` / ``get`` / ``delete`` / ``search`` / ``list_namespaces``
    over ``(namespace_tuple, key)``, where ``search`` filters by namespace prefix (plus an optional
    semantic ``query``, if that implementation enables an index) and returns items ranked by a
    single ``score``; items carry ``created_at`` / ``updated_at`` but those never enter the ranking,
    and TTL is an opt-in capability (``supports_ttl`` defaults to ``False``) whose semantics are
    expiry-deletion.

    The four properties this module exists for sit *outside* that surface, not inside it as
    implementation details: (1) recall ranked by ``relevance × recency × importance`` rather than
    one score; (2) capacity enforcement that evicts by ``retention = recency × importance``;
    (3) tombstones (``deleted_at``) with per-id ``restore``, so eviction is recoverable rather than
    a delete; (4) comparing only vectors from the same embedding space (``model_id``). So this is
    built *beside* ``BaseStore``'s semantics as a supplement, not as a replacement — the memory rows
    live in the same :class:`~owcopilot.storage.sqlite.SQLiteStore` as audit runs and telemetry, so
    no second persistence layer is needed. If these memories ever have to be exposed to the official
    host, the right move is a thin ``BaseStore`` adapter mapping ``put`` / ``get`` / ``search`` onto
    these tables — not folding three-factor ranking and tombstones into the ``BaseStore`` interface.

Namespace / scope (D2 — why ``world_id``, not a checkpointer ``thread_id``):
    LangGraph's long-term-memory guidance requires every entry to live under an explicit namespace
    (its ``Store`` organizes memories under custom namespaces, typically scoped per user/org/app),
    which this module lacked until D2: both tables carried only a ``model_id`` filter, so two
    projects/worlds sharing one ``runtime.sqlite`` file (the multi-world ``(world_id, version)``
    scope dimension already threaded through ``content_index`` and friends — see
    ``storage/sqlite.py``) could recall and evict each other's runs and reflections.

    The first namespace candidate checked was ``storage/checkpointer.py``'s ``thread_id`` convention
    (the other execution-state identifier in this codebase) — but it carries no project component at
    all: :func:`owcopilot.storage.checkpointer.resolve_thread_id` mints a bare UUID (or accepts an
    explicit user string) *per run*, and that module's actual project isolation is a completely
    different mechanism — one independent ``checkpoints.sqlite`` file per ``content_root``, resolved
    in ``cli/main.py`` — not anything encoded inside the id. Aligning with it would mean adopting an
    identifier that is both the wrong grain (per-run, not per-project) and carries no project
    identity to align to in the first place.

    Instead this module reuses the scope dimension ``storage.sqlite.SQLiteStore`` already threads
    through every other table: ``self.world_id``. That IS the project/world boundary in this
    codebase — ``pipeline.project.ProjectContext`` stamps every scoped table with it, and a store
    opened for one project already carries the right value with zero extra plumbing. ``version`` is
    deliberately left out of the memory namespace: an agent's episodic experience of working on a
    world stays a relevant lesson across that world's version branches (unlike content rows, which
    ARE version-specific), so memory namespaces one dimension coarser than content — a single
    ``scope`` column whose default (``'default'``) matches ``world_id``'s own default.

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
    from .react import AgentResult

logger = logging.getLogger(__name__)

#: Cap on the outcome text folded into a run's recall embedding / stored summary. Keeps the vector
#: focused on the goal + headline conclusion rather than a long trailing report.
_SUMMARY_MAX_CHARS = 500

#: Recency half-life. A memory's recency factor is ``0.5 ** (age_hours / half_life)`` — it halves
#: every week, so a day-old and an hour-old memory are near-indistinguishable (gentle for a
#: long-lived narrative project) while a months-old one is heavily discounted.
_RECENCY_HALF_LIFE_HOURS = 168.0

#: Saturation scale for a run's audit-driven importance signal (D3). ``1 - exp(-signal / scale)``
#: reaches ~0.63 at ``scale`` and plateaus toward 1.0. Sized against what one deterministic verdict
#: actually carries: a single ``audit_project`` call on a world with a handful of open errors scores
#: 1 + errors, so a clean-world verdict (signal 1) lands near 0.22 — a real but modest episode — an
#: ordinary "found three problems" run (signal 4) near 0.63, and a heavy find near 1.0.
_IMPORTANCE_SIGNAL_SCALE = 4.0

#: Saturation scale for the PRE-D3 step-count proxy, kept for rows that recorded no audit signal
#: (a run row written before D3, or one saved by a caller that had no ``AgentResult``). Those rows
#: keep their old ranking rather than being silently demoted — the same legacy discipline D1 uses
#: for a never-recalled ``last_accessed_at``.
_IMPORTANCE_STEP_SCALE = 6.0

#: The deterministic checkers a run's importance is read off (D3), and where each one's verdict size
#: lives in its structured tool result. Only these tools produce a machine-recomputable verdict:
#: ``audit_project`` and ``quality_harness`` run the rule engine, ``propose_fix`` reports how many
#: errors a candidate provably clears under shadow validation. Retrieval / graph / read tools are
#: deliberately absent — they return content, not a verdict.
_CHECKER_TOOLS = frozenset({"audit_project", "quality_harness", "propose_fix"})

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
    """One recalled past run (historical-message retrieval): its goal, outcome summary and
    similarity."""

    run_id: str
    goal: str
    summary: str
    similarity: float
    created_at: str


@dataclass(frozen=True)
class ReflectionHit:
    """One recalled reflection (feedback-memory episodic buffer): the lesson and its similarity."""

    goal: str
    reflection: str
    source: str
    similarity: float
    created_at: str


# --------------------------------------------------------------------------- write side


def run_audit_signal(result: AgentResult) -> int:
    """The deterministic-audit signal of one run (D3) — its importance input, in verdict units.

    Sums, over the run's successful steps, every :data:`_CHECKER_TOOLS` call's
    ``1 + <verdict size>``: the ``1`` is "a deterministic verdict was actually obtained" (so a run
    that checked a clean world and correctly found nothing still scores above a run that checked
    nothing at all), and the verdict size is the count of findings or shadow-validated repairs that
    verdict carried (see :func:`_checker_verdict_size`). A run that never reached a checker — the
    long floundering trajectory the pre-D3 proxy over-weighted — scores 0.

    Read off :class:`~owcopilot.agent.react.AgentStep.result`, the exact structured tool payload,
    never the ``observation`` string, which the overflow guard may have truncated mid-JSON. Called
    once per run by :func:`owcopilot.agent.history.persist_agent_run`, which lifts the number onto
    the run row so recall never has to parse a transcript.
    """
    total = 0
    for step in result.steps:
        if step.is_error or not isinstance(step.result, dict):
            continue
        verdict = _checker_verdict_size(step.action, step.result)
        if verdict is not None:
            total += 1 + verdict
    return total


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
    """Persist a distilled ``reflection`` keyed to ``goal`` as a feedback-memory cross-trial
    buffer entry.

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
    except Exception as exc:  # noqa: BLE001 — persistence must not break the feedback-memory retry
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
    """Return the top-``k`` finished past runs most similar to ``goal`` (historical-message
    retrieval).

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
        importance_of=_run_importance,
    )
    hits = [
        EpisodicHit(
            run_id=str(row["run_id"]),
            goal=str(row["goal"]),
            summary=str(row["summary"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in ranked
    ]
    _touch_recalled_runs(store, [hit.run_id for hit in hits])
    return hits


def recall_reflections(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    k: int = 2,
    min_similarity: float = EPISODIC_RECALL_MIN_SIMILARITY,
) -> list[ReflectionHit]:
    """Return the top-``k`` reflections most similar to ``goal`` (feedback-memory episodic buffer).

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
    hits = [
        ReflectionHit(
            goal=str(row["goal"]),
            reflection=str(row["reflection"]),
            source=str(row["source"]),
            similarity=sim,
            created_at=str(row["created_at"]),
        )
        for row, sim in ranked
    ]
    _touch_recalled_reflections(store, [str(row["id"]) for row, _sim in ranked])
    return hits


def format_history_block(hits: list[EpisodicHit]) -> str:
    """Render recalled runs into the prompt context block. Caller guards on non-empty ``hits``."""
    lines = ["## 相关历史任务经验(检索自过往运行,仅供参考)"]
    lines.extend(f"- [{hit.created_at}] 目标:{hit.goal} → 结论:{hit.summary}" for hit in hits)
    return "\n".join(lines)


def recall_agent_memory(
    store: SQLiteStore,
    embedder: Embedder,
    goal: str,
    *,
    enabled: bool = True,
) -> list[str] | None:
    """The recalled episodic context for one agent run, or ``None`` when there is none.

    Combines the two recall channels into the single ``reflections`` list ``ReActAgent.run`` already
    accepts: the historical-message recall similar-run block first (as one context block), then
    the feedback-memory buffer's
    reflections. ``enabled=False`` short-circuits WITHOUT calling recall at all, so the read side is
    genuinely off rather than merely ignored — that is what the CLI's ``--no-recall`` and the
    memory A/B benchmark's OFF arm both mean, and both go through this one function so the two can
    never drift into measuring different things. Fail-open: any recall error is logged and treated
    as no hits, so memory retrieval can never break the agent run. Empty ⇒ ``None``, which leaves
    the prompt byte-identical to a run that has no memory at all."""
    if not enabled:
        return None
    blocks: list[str] = []
    try:
        run_hits = recall_similar_runs(store, embedder, goal)
        if run_hits:
            blocks.append(format_history_block(run_hits))
        blocks.extend(hit.reflection for hit in recall_reflections(store, embedder, goal))
    except Exception as exc:  # noqa: BLE001 — recall is best-effort; never break the run
        logger.warning("agent memory recall failed (%s); running without recalled context", exc)
        return None
    return blocks or None


# --------------------------------------------------------------------------- internals


def _to_blob(vector: list[float]) -> bytes:
    """Serialize an embedding to a float32 blob, matching the retrieval layer's on-disk format."""
    return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


def _checker_verdict_size(action: str, result: dict[str, Any]) -> int | None:
    """How many findings / provable repairs one tool result carries, or ``None`` if ``action`` is
    not a deterministic checker at all.

    ``None`` (not a checker) and ``0`` (a checker that returned an empty verdict) are deliberately
    different: the caller adds a ``+1`` for the latter and nothing for the former. Every read is
    defensive — a payload shape that does not match yields ``0`` rather than raising, because an
    importance signal must never be able to break the write path that computes it.
    """
    if action not in _CHECKER_TOOLS:
        return None
    if action == "audit_project":
        return _non_negative(result.get("open_errors"))
    if action == "quality_harness":
        totals = result.get("audit_totals")
        return _non_negative(totals.get("error")) if isinstance(totals, dict) else 0
    if action == "propose_fix":
        # The candidates are ALTERNATIVE repairs for one issue, so the verdict is the best one's
        # shadow-validated resolved-error count, not their sum — summing would multiply-count a
        # single fixed error by however many candidate routes to it the suggester happened to find.
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            return 0
        return max(
            (
                _non_negative(candidate.get("resolved_error_count"))
                for candidate in candidates
                if isinstance(candidate, dict)
            ),
            default=0,
        )
    # A checker listed in _CHECKER_TOOLS with no size reader yet: count the verdict, not its size.
    # Degrading this way (rather than falling through into another tool's payload shape) is why the
    # membership set and the readers can be edited independently without a silent misread.
    return 0


def _non_negative(value: Any) -> int:
    """A tool payload field read as a count: non-int / negative / missing all read as ``0``."""
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _run_importance(row: dict[str, Any]) -> float:
    """Importance of a past run: a saturating function of the DETERMINISTIC VERDICT it obtained.

    D3. ``audit_signal`` is :func:`run_audit_signal` computed at write time — the audit's own
    machine-recomputable measure of what this run actually established. A short run that ran the
    checker and surfaced a dozen real errors therefore outranks a long one that thrashed through
    retrieval and established nothing, which is precisely the pair the pre-D3 step-count proxy got
    backwards.

    A row with no recorded signal (``audit_signal IS NULL``: written before D3, or by a caller that
    had no ``AgentResult``) falls back to that pre-D3 proxy over ``step_count``, so a legacy row
    keeps exactly its old ranking rather than being silently demoted to the floor. Both branches are
    floored at :data:`_MIN_FACTOR` so even a run that established nothing keeps a small positive
    importance."""
    signal, scale = (
        (int(row["step_count"]), _IMPORTANCE_STEP_SCALE)
        if row.get("audit_signal") is None
        else (int(row["audit_signal"]), _IMPORTANCE_SIGNAL_SCALE)
    )
    return max(_MIN_FACTOR, 1.0 - math.exp(-max(0, signal) / scale))


def _recency(timestamp: str, now: datetime) -> float:
    """Recency factor ``0.5 ** (age_hours / half_life)`` — halves each ``_RECENCY_HALF_LIFE_HOURS``.

    ``timestamp`` is whatever :func:`_recency_basis` picked (last access, or creation as a
    fallback) — this function itself is agnostic to which. Fail-open on an unparseable / future
    timestamp: unparseable is treated as fresh (factor 1.0) rather than crashing recall, and a
    future timestamp clamps age to 0."""
    ts = _parse_iso(timestamp)
    if ts is None:
        return 1.0
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return max(_MIN_FACTOR, 0.5 ** (age_hours / _RECENCY_HALF_LIFE_HOURS))


def _recency_basis(row: dict[str, Any]) -> str:
    """D1: the timestamp recency decays from — a memory's last recall touch if it has one, else its
    write time.

    three-factor retrieval  defines recency as decay since the memory's most recent
    *access*, not its creation. ``last_accessed_at`` is NULL until :func:`_touch_recalled_runs` /
  :func:`_touch_recalled_reflections` first stamps it on a successful recall, so a never-recalled
    row falls back to ``created_at`` — its only timestamp, and today's exact pre-D1 behaviour."""
    accessed = row.get("last_accessed_at")
    return str(accessed) if accessed else str(row["created_at"])


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
        blended = cosine * _recency(_recency_basis(row), now) * importance_of(row)
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


# --------------------------------------------------------------------------- D1: recency write-back


def _touch_recalled_runs(store: SQLiteStore, run_ids: list[str]) -> None:
    """Reset the recency clock on every run embedding actually returned by a recall (D1).

    Best-effort, same fail-open discipline as the rest of this module's writes: a touch failure is
    logged and swallowed rather than raised, so it can never turn a successful recall into a broken
    one. No-op on an empty list (nothing was recalled)."""
    if not run_ids:
        return
    try:
        store.touch_agent_run_embeddings(run_ids)
    except Exception as exc:  # noqa: BLE001 — recency touch is best-effort; never break recall
        logger.warning("failed to touch recency for recalled runs %s: %s", run_ids, exc)


def _touch_recalled_reflections(store: SQLiteStore, reflection_ids: list[str]) -> None:
    """Reset the recency clock on every reflection actually returned by a recall (D1).

    Same best-effort discipline as :func:`_touch_recalled_runs`."""
    if not reflection_ids:
        return
    try:
        store.touch_reflections(reflection_ids)
    except Exception as exc:  # noqa: BLE001 — recency touch is best-effort; never break recall
        logger.warning(
            "failed to touch recency for recalled reflections %s: %s", reflection_ids, exc
        )


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
            key=lambda row: _recency(_recency_basis(row), now) * _run_importance(row),
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
            key=lambda row: _recency(_recency_basis(row), now) * _REFLECTION_IMPORTANCE,
        )
        evict = [str(row["id"]) for row in ordered[: len(rows) - capacity]]
        store.soft_delete_reflections(evict)
    except Exception as exc:  # noqa: BLE001 — capacity sweep is best-effort; never break the flow
        logger.warning("episodic reflection-capacity sweep failed for space %s: %s", model_id, exc)


# WEAKNESSES (honest degradations of the three-factor lifecycle):
#   * importance for RUNS is the DETERMINISTIC AUDIT SIGNAL (D3), not the LLM-scored poignancy of
# three-factor retrieval. What it measures is what the run *verified*, which is deliberate — it is
#     recomputable from the transcript by anyone, and it costs no extra model call on the fail-open
#     write path. What it therefore does NOT measure: a run whose value was a judgement call no
#     checker can express (a good triage of an ambiguous world, a well-argued "leave this alone")
#     scores at the floor, the same as a run that established nothing. A future poignancy score can
#     still replace `_run_importance` without touching either call site.
#   * a run row with no recorded signal keeps the PRE-D3 step-count proxy (see `_run_importance`),
#     so the two biases D3 fixes — a short pivotal run under-weighted, a long floundering one
#     over-weighted — are still present for rows written before D3. That is the deliberate
#     migration choice: a legacy row's ranking is preserved rather than silently zeroed.
#   * importance for REFLECTIONS is a constant — there is no per-item signal to rank one distilled
#     lesson above another — so reflection recall ranks on relevance × recency only.
#   * capacity bounds the *stored volume* per embedding space (all live embeddings, finished or
#     not), not only the recall-eligible finished set; an unfinished high-step embedding can occupy
#     a slot it is never recalled into. Acceptable: it still bounds growth, unfinished runs are few.
#   * recovery is per-id (`store.restore_agent_run_embedding` / `restore_reflection`): a deliberate,
#     targeted un-tombstone; there is no bulk "undo last sweep", since eviction is by design.
