"""Weak-supervision preference signal export from the review queue.

Exports two classes of supervision signal to JSONL for downstream research use:

1. **Critique-guided incomplete pairs** — items whose ``refine_trail`` has >=2 rounds
   (at least one ``verdict="revise"`` followed by a final ``verdict="pass"``).
   Chosen payload is the final artifact; the rejected artifact is NOT recoverable
   (``RefineStep`` does not store intermediate artifact snapshots), so only the
   rejected critique signal (score + fixes) is exported.

   Confidence = 0.5.  Source = "critique_guided_incomplete".

2. **Human-review single samples** — items with ``status in {"accepted", "rejected"}``.
   Label maps accepted→"chosen" / rejected→"rejected".  No paired counterpart for
   the same prompt, so these cannot be used directly for standard DPO training.

   Confidence = 0.9.  Source = "human_review".

诚实标注：
- 这不是"人类偏好"偏好对，不适合直接用于标准 DPO 训练。
- confidence 值 0.5 / 0.9 反映数据完整性，不代表标签质量，不得上调。
- 代码/注释中禁止出现"training-ready"/"DPO training dataset"/"human preference pair"等夸大表述。
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
    仅适合用于 RAFT-style critic-guided SFT，不可用于标准 DPO。
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
        "Suitable for RAFT-style critic-guided SFT only, NOT standard DPO."
    )


class PreferenceSample(BaseModel):
    """Human-review single-side sample.

    诚实标注：label 来自人审决策（accepted/rejected），但无同 prompt 的对立样本，
    不可直接用于标准 DPO 训练。confidence=0.9，不得上调。
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
        "Cannot be used directly for standard DPO training."
    )


class DatasetMetadata(BaseModel):
    """Export metadata — must report data quality honestly.

    data_quality_note 必须含 "WEAK supervision" 以诚实反映数据性质。
    warn_empty=True 当无任何样本时（不 raise，只发 warning）。
    """

    generated_at: str = ""
    total_pairs: int = 0
    total_samples: int = 0
    pairs_by_item_type: dict[str, int] = Field(default_factory=dict)
    samples_by_item_type: dict[str, int] = Field(default_factory=dict)
    samples_by_label: dict[str, int] = Field(default_factory=dict)
    confidence_distribution: dict[str, int] = Field(default_factory=dict)
    data_quality_note: str = (
        "This dataset contains two classes of WEAK supervision signals: "
        "(a) AI-critic-guided improvement pairs with incomplete rejected artifact "
        "(confidence=0.5, source=critique_guided_incomplete; analogous to RAFT/Constitutional AI, "
        "NOT suitable for standard DPO without the missing rejected text); "
        "(b) Human-review single samples lacking a paired counterpart for the same prompt "
        "(confidence=0.9, source=human_review; may serve as a weak signal for reward-model "
        "training or single-side classification, subject to scale caveats; NOT standard DPO). "
        "Sample count is expected to be small for development-scale projects. "
        "This is an architecture demonstration, not a production dataset."
    )
    warn_empty: bool = False


class PreferenceDataset(BaseModel):
    """Exported preference dataset — two WEAK supervision signal classes."""

    pairs: list[PreferencePair] = Field(default_factory=list)
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
    samples: list[PreferenceSample] = []

    for item in items:
        payload = item.get("payload") or {}
        item_type = item.get("item_type", "")
        object_ref = item.get("object_ref", "")
        status = item.get("status", "")

        # ── Critique-guided pair extraction ────────────────────────────────
        # Filter: trail >= 2 steps, at least one "revise", last step is "pass".
        # Matches C2-H1 / H2 / H3 contract.
        trail_raw = payload.get("refine_trail")
        if isinstance(trail_raw, list) and len(trail_raw) >= 2:
            trail = trail_raw
            verdicts = [step.get("verdict", "") for step in trail]
            last_verdict = verdicts[-1] if verdicts else ""
            has_revise = "revise" in verdicts

            if has_revise and last_verdict == "pass":
                # First revise step provides the "rejected" critique signal
                first_revise_idx = next(
                    (i for i, v in enumerate(verdicts) if v == "revise"), None
                )
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

        # ── Human-review single-side sample extraction ────────────────────
        # Only accepted/rejected status (C2-H4); pending_review is excluded.
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

    metadata = _build_metadata(pairs, samples)
    dataset = PreferenceDataset(pairs=pairs, samples=samples, metadata=metadata)

    if output_dir is not None:
        _write_jsonl(Path(output_dir), dataset)

    return dataset


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_metadata(
    pairs: list[PreferencePair],
    samples: list[PreferenceSample],
) -> DatasetMetadata:
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
    for s in samples:
        key = str(s.confidence)
        conf_dist[key] = conf_dist.get(key, 0) + 1

    total_pairs = len(pairs)
    total_samples = len(samples)
    warn_empty = total_pairs == 0 and total_samples == 0

    if warn_empty:
        warnings.warn(
            "export_preference_dataset: no pairs and no samples found. "
            "The review store may be empty or all items have been filtered out. "
            "Returning empty PreferenceDataset with warn_empty=True.",
            stacklevel=4,
        )
        logger.warning(
            "DPO export: 0 pairs, 0 samples. Store may be empty or all items filtered."
        )

    return DatasetMetadata(
        generated_at=datetime.now(UTC).isoformat(),
        total_pairs=total_pairs,
        total_samples=total_samples,
        pairs_by_item_type=pairs_by_type,
        samples_by_item_type=samples_by_type,
        samples_by_label=samples_by_label,
        confidence_distribution=conf_dist,
        warn_empty=warn_empty,
    )


def _write_jsonl(output_dir: Path, dataset: PreferenceDataset) -> None:
    """Write pairs JSONL, samples JSONL, and metadata JSON to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs_path = output_dir / "critique_guided_pairs.jsonl"
    with pairs_path.open("w", encoding="utf-8") as f:
        for pair in dataset.pairs:
            f.write(pair.model_dump_json() + "\n")

    samples_path = output_dir / "human_signal_samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as f:
        for sample in dataset.samples:
            f.write(sample.model_dump_json() + "\n")

    meta_path = output_dir / "metadata.json"
    meta_path.write_text(
        json.dumps(dataset.metadata.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
