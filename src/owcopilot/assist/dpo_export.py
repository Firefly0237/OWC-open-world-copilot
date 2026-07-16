# 从审阅队列导出偏好数据：这是数据闭环/训练素材交接层，不会微调模型或产出权重。
"""Preference-data export from the review queue.

This module is a **data pipeline**, not a training pipeline: it reads already-persisted
review/refine traces and writes JSONL files for downstream analysis or optional future model
alignment work. It does not call a trainer, fine-tune a model, produce weights, or switch the
runtime LLM to a trained checkpoint.

Exports three classes of supervision signal to JSONL for downstream research/use:

1. **Critique-guided incomplete pairs** — items whose ``refine_trail`` has >=2 rounds
   (at least one ``verdict="revise"`` followed by a final ``verdict="pass"``).
   Chosen payload is the final artifact; the rejected artifact is NOT recoverable
   (``RefineStep`` does not store intermediate artifact snapshots), so only the
   rejected critique signal (score + fixes) is exported.

   Confidence = 0.5.  Source = "critique_guided_incomplete".

2. **Human-review single samples** — items with ``status in {"accepted", "rejected"}``.
   Label maps accepted→"chosen" / rejected→"rejected".  No paired counterpart for
   the same prompt, so these cannot be used directly as standard DPO trainer input.

   Confidence = 0.9.  Source = "human_review".

3. **Complete improvement pairs** — newer refine trails with ``artifact_before`` snapshots.
   These contain both ``prompt`` / ``chosen`` / ``rejected`` and are formatted so a downstream
   DPO-style trainer can consume them. This repo still only exports the data; any actual
   training/evaluation run lives outside this module.

诚实标注：
- 这是偏好数据导出/数据闭环能力，不是模型训练或微调能力。
- 不完整对与单边样本不是"人类偏好"标准 DPO 对，不适合直接作为标准 DPO 输入。
- complete_pairs 只是下游 DPO/偏好训练器可消费的数据格式；本模块不训练模型、不产出权重。
- confidence 值 0.5 / 0.9 反映数据完整性，不代表标签质量，不得上调。
- 代码/注释中避免使用“训练就绪、已完成微调、真实人类偏好训练集”等夸大表述。
- source 字段必须含 "_incomplete" 以诚实反映数据不完整性。
- data_note 字段每条必须写明"rejected artifact unrecoverable"（pairs）或"no paired"（samples）。
- DatasetMetadata.data_quality_note 必须含"WEAK supervision"。

$0 无 LLM 调用。0 样本时 warn_empty=True 但不 raise。
"""

from __future__ import annotations

import json
import logging
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


# ── Data structures ──────────────────────────────────────────────────────────


class PreferencePair(BaseModel):
    """Incomplete critique-guided improvement signal.

    诚实标注：chosen payload 已知，rejected artifact **不可恢复**
    （RefineStep 不存储中间 artifact 快照），因此这是不完整偏好对。
    仅适合用于离线分析或 RAFT-style critic-guided SFT 实验，不可直接作为标准 DPO 输入。
    confidence=0.5 反映数据不完整性，不得上调。
    """

    pair_id: str
    item_type: str
    object_ref: str
    chosen_payload: dict[str, Any]
    chosen_score: float
    rejected_score: float
    rejected_fixes: list[str] = Field(default_factory=list)
    trail_length: int
    critic_primary_dim: str | None = None
    # source 必须含 "_incomplete"，诚实反映无 rejected artifact
    source: str = "critique_guided_incomplete"
    # confidence=0.5 — 无 rejected text，置信度仅 0.5，不得上调
    confidence: float = 0.5
    data_note: str = (
        "Incomplete pair: chosen payload available, "
        "rejected artifact unrecoverable "
        "(RefineStep does not store intermediate artifact snapshots). "
        "Suitable for offline analysis or RAFT-style critic-guided SFT experiments only; "
        "NOT standard DPO trainer input."
    )


class CompletePreferencePair(BaseModel):
    """Complete improvement pair — BOTH sides present for downstream DPO-style tooling.

    Recoverable once ``RefineStep.artifact_before`` snapshots landed: ``rejected`` is the
    pre-revision artifact the critic sent back, ``chosen`` is what that same revision round
    produced (the next snapshot, or the final passing payload), ``prompt`` carries the actual
    revision instructions (fixes + reflection) that drove the edit. Improvement-pair semantics:
    under the same task, the post-revision artifact is preferred over the pre-revision one.

    confidence: 0.9 when a human ACCEPTED the final artifact (both sides + preference are
    human-anchored); 0.7 when the preference comes from the critic's pass verdict only.
    Old trails without snapshots still export as :class:`PreferencePair` (incomplete) — the
    honest labelling of that class is unchanged. This is a handoff row, not proof that any model
    has been trained or fine-tuned.
    """

    pair_id: str
    item_type: str
    object_ref: str
    prompt: str
    chosen: dict[str, Any]
    rejected: dict[str, Any]
    chosen_score: float
    rejected_score: float
    round_index: int
    human_confirmed: bool = False
    source: str = "critique_guided_complete"
    confidence: float = 0.7
    data_note: str = (
        "Complete improvement pair: rejected = pre-revision artifact snapshot, "
        "chosen = the artifact that revision round produced, prompt = the revision "
        "instructions that drove the edit. DPO-format compatible handoff data only; "
        "OWCopilot does not train or fine-tune a model here."
    )


class PreferenceSample(BaseModel):
    """Human-review single-side sample.

    诚实标注：label 来自人审决策（accepted/rejected），但无同 prompt 的对立样本，
    不可直接作为标准 DPO 输入。confidence=0.9，不得上调。
    """

    sample_id: str
    item_type: str
    object_ref: str
    payload: dict[str, Any]
    label: str  # "chosen" (accepted) | "rejected"
    source: str = "human_review"
    # confidence=0.9 — 人审决策，置信度 0.9，不得上调
    confidence: float = 0.9
    critic_verdict: str | None = None
    critic_score: float | None = None
    critic_primary_dim: str | None = None
    data_note: str = (
        "Human-review single sample; no paired rejected/chosen for same prompt. "
        "Cannot be used directly as standard DPO trainer input."
    )


class DatasetMetadata(BaseModel):
    """Export metadata — must report data quality honestly.

    data_quality_note 必须含 "WEAK supervision" 以诚实反映数据性质。
    warn_empty=True 当无任何样本时（不 raise，只发 warning）。
    """

    generated_at: str = ""
    total_pairs: int = 0
    total_complete_pairs: int = 0  # DPO-format handoff rows (both sides present; no training here)
    total_samples: int = 0
    pairs_by_item_type: dict[str, int] = Field(default_factory=dict)
    samples_by_item_type: dict[str, int] = Field(default_factory=dict)
    samples_by_label: dict[str, int] = Field(default_factory=dict)
    confidence_distribution: dict[str, int] = Field(default_factory=dict)
    data_quality_note: str = (
        "This dataset contains two classes of WEAK supervision signals: "
        "(a) AI-critic-guided improvement pairs with incomplete rejected artifact "
        "(confidence=0.5, source=critique_guided_incomplete; analogous to RAFT/Constitutional AI, "
        "NOT suitable as standard DPO trainer input without the missing rejected text); "
        "(b) Human-review single samples lacking a paired counterpart for the same prompt "
        "(confidence=0.9, source=human_review; may serve as a weak signal for offline preference "
        "analysis or single-side classification, subject to scale caveats; NOT standard DPO). "
        "A third class, complete_pairs (source=critique_guided_complete), carries BOTH sides "
        "(pre-revision snapshot vs post-revision artifact, improvement-pair semantics) and is "
        "DPO-format compatible for downstream experiments. This export does NOT train, fine-tune, "
        "produce model weights, or change OWCopilot's runtime model. It only exists for trails "
        "recorded after RefineStep.artifact_before landed. Sample count is expected to be small "
        "for development-scale projects. This is a data-pipeline demonstration, not a production "
        "training dataset."
    )
    warn_empty: bool = False


class PreferenceDataset(BaseModel):
    """Exported preference dataset — two WEAK signal classes + the complete-pair class."""

    pairs: list[PreferencePair] = Field(default_factory=list)
    complete_pairs: list[CompletePreferencePair] = Field(default_factory=list)
    samples: list[PreferenceSample] = Field(default_factory=list)
    metadata: DatasetMetadata = Field(default_factory=DatasetMetadata)


# ── Core extraction logic ─────────────────────────────────────────────────────


def export_preference_dataset(
    store: SQLiteStore,
    *,
    output_dir: str | Path | None = None,
) -> PreferenceDataset:
    """Extract weak-supervision signals from the review queue and optionally write JSONL.

    Pure SQLite read — $0, no LLM call, no external network access.
    0 samples → warn_empty=True in metadata, no exception raised.

    Args:
        store: An open SQLiteStore instance (read-only usage).
        output_dir: If given, write critique_guided_pairs.jsonl,
            human_signal_samples.jsonl, and metadata.json to this directory.

    Returns:
        PreferenceDataset with pairs, samples, and metadata populated.
    """
    items = store.list_review_items()
    pairs: list[PreferencePair] = []
    complete_pairs: list[CompletePreferencePair] = []
    samples: list[PreferenceSample] = []

    for item in items:
        payload = item.get("payload") or {}
        item_type = item.get("item_type", "")
        object_ref = item.get("object_ref", "")
        status = item.get("status", "")

        # ── Critique-guided pair extraction ────────────────────────────────
        # Filter: trail >= 2 steps, at least one "revise", last step is "pass".
        # Matches the incomplete preference-signal contract (a revise-then-pass trail).
        trail_raw = payload.get("refine_trail")
        if isinstance(trail_raw, list) and len(trail_raw) >= 2:
            trail = trail_raw
            verdicts = [step.get("verdict", "") for step in trail]
            last_verdict = verdicts[-1] if verdicts else ""
            has_revise = "revise" in verdicts

            if has_revise and last_verdict == "pass":
                # First revise step provides the "rejected" critique signal
                first_revise_idx = next((i for i, v in enumerate(verdicts) if v == "revise"), None)
                if first_revise_idx is not None:
                    first_revise_step = trail[first_revise_idx]
                    last_pass_step = trail[-1]
                    pairs.append(
                        PreferencePair(
                            pair_id=f"{item['id']}_pair",
                            item_type=item_type,
                            object_ref=object_ref,
                            chosen_payload=payload,
                            chosen_score=float(last_pass_step.get("score", 0.0)),
                            rejected_score=float(first_revise_step.get("score", 0.0)),
                            rejected_fixes=list(first_revise_step.get("fixes", [])),
                            trail_length=len(trail),
                            critic_primary_dim=item.get("critic_primary_dim"),
                        )
                    )
            # Complete pairs (independent of the last=pass filter above): adjacent-snapshot
            # pairs are anchored by the critic score RISING across the revision, so they are
            # valid preference signal even on a trail that ran out of refine budget before
            # passing; only the last-snapshot→final-payload pair requires the final artifact
            # to have actually passed. Old trails have no snapshots and simply yield none.
            complete_pairs.extend(
                _complete_pairs_from_trail(
                    trail,
                    item_id=str(item["id"]),
                    item_type=item_type,
                    object_ref=object_ref,
                    final_payload=payload,
                    final_passed=last_verdict == "pass",
                    human_confirmed=status == "accepted",
                )
            )

        # ── Human-review single-side sample extraction ────────────────────
        # Only accepted/rejected status; pending_review is excluded.
        if status in ("accepted", "rejected"):
            label = "chosen" if status == "accepted" else "rejected"
            samples.append(
                PreferenceSample(
                    sample_id=f"{item['id']}_sample",
                    item_type=item_type,
                    object_ref=object_ref,
                    payload=payload,
                    label=label,
                    critic_verdict=item.get("critic_verdict"),
                    critic_score=(
                        float(item["critic_score"])
                        if item.get("critic_score") is not None
                        else None
                    ),
                    critic_primary_dim=item.get("critic_primary_dim"),
                )
            )

    metadata = _build_metadata(pairs, samples, complete_pairs=complete_pairs)
    dataset = PreferenceDataset(
        pairs=pairs, complete_pairs=complete_pairs, samples=samples, metadata=metadata
    )

    if output_dir is not None:
        _write_jsonl(Path(output_dir), dataset)

    return dataset


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_metadata(
    pairs: list[PreferencePair],
    samples: list[PreferenceSample],
    *,
    complete_pairs: list[CompletePreferencePair] | None = None,
) -> DatasetMetadata:
    complete_pairs = complete_pairs or []
    pairs_by_type: dict[str, int] = {}
    for p in pairs:
        pairs_by_type[p.item_type] = pairs_by_type.get(p.item_type, 0) + 1

    samples_by_type: dict[str, int] = {}
    samples_by_label: dict[str, int] = {}
    for s in samples:
        samples_by_type[s.item_type] = samples_by_type.get(s.item_type, 0) + 1
        samples_by_label[s.label] = samples_by_label.get(s.label, 0) + 1

    conf_dist: dict[str, int] = {}
    for p in pairs:
        key = str(p.confidence)
        conf_dist[key] = conf_dist.get(key, 0) + 1
    for cp in complete_pairs:
        key = str(cp.confidence)
        conf_dist[key] = conf_dist.get(key, 0) + 1
    for s in samples:
        key = str(s.confidence)
        conf_dist[key] = conf_dist.get(key, 0) + 1

    total_pairs = len(pairs)
    total_samples = len(samples)
    warn_empty = total_pairs == 0 and total_samples == 0 and not complete_pairs

    if warn_empty:
        warnings.warn(
            "export_preference_dataset: no pairs and no samples found. "
            "The review store may be empty or all items have been filtered out. "
            "Returning empty PreferenceDataset with warn_empty=True.",
            stacklevel=4,
        )
        logger.warning(
            "preference export: 0 pairs, 0 samples. Store may be empty or all items filtered."
        )

    return DatasetMetadata(
        generated_at=datetime.now(UTC).isoformat(),
        total_pairs=total_pairs,
        total_complete_pairs=len(complete_pairs),
        total_samples=total_samples,
        pairs_by_item_type=pairs_by_type,
        samples_by_item_type=samples_by_type,
        samples_by_label=samples_by_label,
        confidence_distribution=conf_dist,
        warn_empty=warn_empty,
    )


def _complete_pairs_from_trail(
    trail: list[dict[str, Any]],
    *,
    item_id: str,
    item_type: str,
    object_ref: str,
    final_payload: dict[str, Any],
    final_passed: bool,
    human_confirmed: bool,
) -> list[CompletePreferencePair]:
    """Build complete improvement pairs from the snapshot-carrying revise steps of one trail.

    For each step ``i`` with an ``artifact_before`` snapshot: rejected = that snapshot,
    chosen = the NEXT snapshot (what this revision produced) or, for the last snapshot-carrying
    step, the final payload. Preference anchoring is per-pair: an adjacent-snapshot pair is
    emitted only when the critic score ROSE across the revision (the score delta IS the
    preference signal, so a passing final verdict is not required); the last-snapshot→final
    pair is emitted only when the final artifact actually passed (``final_passed``) — a
    still-failing final draft anchors no preference. Steps without snapshots (all pre-existing
    trails) yield nothing — the incomplete-pair class keeps covering those.
    """
    snap_indices = [
        i for i, step in enumerate(trail) if isinstance(step.get("artifact_before"), dict)
    ]
    out: list[CompletePreferencePair] = []
    final_score = float(trail[-1].get("score", 0.0)) if trail else 0.0
    for pos, i in enumerate(snap_indices):
        step = trail[i]
        rejected = step["artifact_before"]
        rejected_score = float(step.get("score", 0.0))
        if pos + 1 < len(snap_indices):
            nxt = trail[snap_indices[pos + 1]]
            chosen = nxt["artifact_before"]
            chosen_score = float(nxt.get("score", 0.0))
            if chosen_score <= rejected_score:
                continue  # no score rise → no preference signal for this revision
        else:
            if not final_passed:
                continue  # final draft never passed → nothing anchors this pair
            chosen = final_payload
            chosen_score = final_score
        fixes = [str(fix) for fix in step.get("fixes", [])]
        reflection = str(step.get("reflection", "") or "")
        prompt = (
            f"修订以下 {item_type}（{object_ref}）。修订要求：\n- "
            + "\n- ".join(fixes or ["（无具体修订项）"])
            + (f"\n反思：{reflection}" if reflection else "")
        )
        out.append(
            CompletePreferencePair(
                pair_id=f"{item_id}_complete_{i}",
                item_type=item_type,
                object_ref=object_ref,
                prompt=prompt,
                chosen=chosen,
                rejected=rejected,
                chosen_score=chosen_score,
                rejected_score=rejected_score,
                round_index=int(step.get("round", i)),
                human_confirmed=human_confirmed,
                confidence=0.9 if human_confirmed else 0.7,
            )
        )
    return out


def _write_jsonl(output_dir: Path, dataset: PreferenceDataset) -> None:
    """Write pairs JSONL, samples JSONL, and metadata JSON to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs_path = output_dir / "critique_guided_pairs.jsonl"
    with pairs_path.open("w", encoding="utf-8") as f:
        for pair in dataset.pairs:
            f.write(pair.model_dump_json() + "\n")

    # Complete pairs: top-level prompt/chosen/rejected as STRINGS (chosen/rejected JSON-encoded)
    # so downstream DPO-style tooling can consume the file directly. This module only writes the
    # handoff data; it does not invoke TRL/DPOTrainer or train/fine-tune any model.
    complete_path = output_dir / "complete_pairs.jsonl"
    with complete_path.open("w", encoding="utf-8") as f:
        for cp in dataset.complete_pairs:
            row = {
                "prompt": cp.prompt,
                "chosen": json.dumps(cp.chosen, ensure_ascii=False, sort_keys=True),
                "rejected": json.dumps(cp.rejected, ensure_ascii=False, sort_keys=True),
                "meta": cp.model_dump(mode="json"),
            }
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    samples_path = output_dir / "human_signal_samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as f:
        for sample in dataset.samples:
            f.write(sample.model_dump_json() + "\n")

    meta_path = output_dir / "metadata.json"
    meta_path.write_text(
        json.dumps(dataset.metadata.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
