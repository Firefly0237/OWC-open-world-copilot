"""Tests for C2: assist/dpo_export.py — weak-supervision preference signal export.

诚实标注覆盖 (C2-H1 through C2-H12):
- H1: trail<2 filtered out
- H2: trail with no "revise" filtered out
- H3: trail with non-pass last step filtered out
- H4: pending_review not in samples
- H5: label mapping accepted→chosen, rejected→rejected
- H6: empty store → warn_empty=True, no exception
- H7: confidence=0.5 (pairs) / 0.9 (samples) — exact values, not upper-bounded
- H8: data_note contains "rejected artifact unrecoverable" (pairs) / "no paired" (samples)
      metadata.data_quality_note contains "WEAK supervision"
- H9: source == "critique_guided_incomplete" (pairs) / "human_review" (samples)
- H10: no LLM calls
- H11: trail_length == len(refine_trail) (not hardcoded)
- H12: (C4 dependency — validated in test_voice_card.py)
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from owcopilot.assist.dpo_export import (
    DatasetMetadata,
    PreferenceDataset,
    PreferencePair,
    PreferenceSample,
    export_preference_dataset,
)
from owcopilot.storage.sqlite import SQLiteStore

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _save_item(
    store: SQLiteStore,
    *,
    item_id: str = "item_001",
    item_type: str = "bark_variant",
    object_ref: str = "bark:npc_r1_a:1",
    status: str = "pending_review",
    trail: list[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {}
    if trail is not None:
        payload["refine_trail"] = trail

    store.save_review_item(
        {
            "id": item_id,
            "item_type": item_type,
            "object_ref": object_ref,
            "payload": payload,
            "issue_refs": [],
            "status": status,
        }
    )


def _valid_trail() -> list[dict[str, Any]]:
    """A valid trail with revise→pass (qualifies for pair extraction)."""
    return [
        {"round": 0, "verdict": "revise", "score": 0.52,
         "fixes": ["improve voice"], "reflection": ""},
        {"round": 1, "verdict": "pass", "score": 0.88, "fixes": [], "reflection": ""},
    ]


# ── H1: trail_length < 2 filtered ─────────────────────────────────────────────


def test_export_filters_trail_length_1_h1() -> None:
    """C2-H1: trail_length=1 item does NOT appear in pairs."""
    store = _make_store()
    _save_item(store, trail=[
        {"round": 0, "verdict": "pass", "score": 0.85, "fixes": [], "reflection": ""},
    ])
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 0


def test_export_filters_empty_trail_h1() -> None:
    """C2-H1: empty trail → not in pairs."""
    store = _make_store()
    _save_item(store, trail=[])
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 0


def test_export_filters_no_trail_field_h1() -> None:
    """C2-H1: no refine_trail key in payload → not in pairs."""
    store = _make_store()
    _save_item(store, trail=None)
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 0


# ── H2: trail with no "revise" filtered ───────────────────────────────────────


def test_export_filters_all_pass_trail_h2() -> None:
    """C2-H2: trail with all verdicts 'pass' (no revise) → not in pairs."""
    store = _make_store()
    _save_item(store, trail=[
        {"round": 0, "verdict": "pass", "score": 0.85, "fixes": [], "reflection": ""},
        {"round": 1, "verdict": "pass", "score": 0.92, "fixes": [], "reflection": ""},
    ])
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 0


# ── H3: trail with non-pass last step filtered ────────────────────────────────


def test_export_filters_non_pass_last_step_h3() -> None:
    """C2-H3: trail ending in 'revise' → not in pairs."""
    store = _make_store()
    _save_item(store, trail=[
        {"round": 0, "verdict": "revise", "score": 0.5, "fixes": ["fix voice"], "reflection": ""},
        {"round": 1, "verdict": "revise", "score": 0.6, "fixes": ["fix more"], "reflection": ""},
    ])
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 0


# ── Valid pair extraction ──────────────────────────────────────────────────────


def test_export_valid_pair_extracted() -> None:
    """Valid trail (revise→pass) produces one pair with correct fields."""
    store = _make_store()
    _save_item(store, item_id="item_valid", trail=_valid_trail())
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 1
    pair = dataset.pairs[0]
    assert pair.pair_id == "item_valid_pair"
    assert pair.trail_length == 2
    assert pair.rejected_score == pytest.approx(0.52)
    assert pair.chosen_score == pytest.approx(0.88)
    assert pair.rejected_fixes == ["improve voice"]


def test_export_trail_length_accurate_h11() -> None:
    """C2-H11: trail_length == len(refine_trail) — not hardcoded."""
    store = _make_store()
    long_trail = [
        {"round": 0, "verdict": "revise", "score": 0.4, "fixes": ["a"], "reflection": ""},
        {"round": 1, "verdict": "revise", "score": 0.6, "fixes": ["b"], "reflection": ""},
        {"round": 2, "verdict": "pass", "score": 0.9, "fixes": [], "reflection": ""},
    ]
    _save_item(store, item_id="item_long", trail=long_trail)
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 1
    assert dataset.pairs[0].trail_length == 3


# ── H4 / H5: human_signal_samples status filtering ───────────────────────────


def test_export_pending_review_not_in_samples_h4() -> None:
    """C2-H4: pending_review items do NOT appear in samples."""
    store = _make_store()
    _save_item(store, item_id="pend_1", status="pending_review")
    dataset = export_preference_dataset(store)
    assert len(dataset.samples) == 0


def test_export_accepted_maps_to_chosen_h5() -> None:
    """C2-H5: status=accepted → label='chosen'."""
    store = _make_store()
    _save_item(store, item_id="acc_1", status="accepted")
    dataset = export_preference_dataset(store)
    assert len(dataset.samples) == 1
    assert dataset.samples[0].label == "chosen"


def test_export_rejected_maps_to_rejected_h5() -> None:
    """C2-H5: status=rejected → label='rejected'."""
    store = _make_store()
    _save_item(store, item_id="rej_1", status="rejected")
    dataset = export_preference_dataset(store)
    assert len(dataset.samples) == 1
    assert dataset.samples[0].label == "rejected"


def test_export_sample_id_format() -> None:
    """sample_id must be '{item_id}_sample'."""
    store = _make_store()
    _save_item(store, item_id="my_item", status="accepted")
    dataset = export_preference_dataset(store)
    assert dataset.samples[0].sample_id == "my_item_sample"


# ── H6: empty store → warn_empty=True, no exception ──────────────────────────


def test_export_empty_store_no_error_h6() -> None:
    """C2-H6: empty store → PreferenceDataset returned, no exception."""
    store = _make_store()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        dataset = export_preference_dataset(store)
    assert isinstance(dataset, PreferenceDataset)
    assert dataset.metadata.warn_empty is True
    assert dataset.metadata.total_pairs == 0
    assert dataset.metadata.total_samples == 0
    # A warning should have been issued
    assert any(
        "empty" in str(warning.message).lower() or "0" in str(warning.message)
        for warning in w
    )


# ── H7: confidence values exact ───────────────────────────────────────────────


def test_pair_confidence_exactly_05_h7() -> None:
    """C2-H7: PreferencePair.confidence == 0.5 exactly (not 0.6 or higher)."""
    store = _make_store()
    _save_item(store, item_id="p_conf", trail=_valid_trail())
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 1
    assert dataset.pairs[0].confidence == 0.5


def test_sample_confidence_exactly_09_h7() -> None:
    """C2-H7: PreferenceSample.confidence == 0.9 exactly."""
    store = _make_store()
    _save_item(store, item_id="s_conf", status="accepted")
    dataset = export_preference_dataset(store)
    assert len(dataset.samples) == 1
    assert dataset.samples[0].confidence == 0.9


# ── H8: data_note and data_quality_note ───────────────────────────────────────


def test_pair_data_note_contains_unrecoverable_h8() -> None:
    """C2-H8: PreferencePair.data_note contains 'rejected artifact unrecoverable'."""
    store = _make_store()
    _save_item(store, item_id="dn_pair", trail=_valid_trail())
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 1
    assert "rejected artifact unrecoverable" in dataset.pairs[0].data_note


def test_sample_data_note_contains_no_paired_h8() -> None:
    """C2-H8: PreferenceSample.data_note contains 'no paired'."""
    store = _make_store()
    _save_item(store, item_id="dn_samp", status="accepted")
    dataset = export_preference_dataset(store)
    assert len(dataset.samples) == 1
    assert "no paired" in dataset.samples[0].data_note


def test_metadata_quality_note_contains_weak_supervision_h8() -> None:
    """C2-H8: DatasetMetadata.data_quality_note contains 'WEAK supervision'."""
    store = _make_store()
    dataset = export_preference_dataset(store)
    assert "WEAK supervision" in dataset.metadata.data_quality_note


# ── H9: source fields ─────────────────────────────────────────────────────────


def test_pair_source_contains_incomplete_h9() -> None:
    """C2-H9: PreferencePair.source == 'critique_guided_incomplete' (contains _incomplete)."""
    store = _make_store()
    _save_item(store, item_id="src_pair", trail=_valid_trail())
    dataset = export_preference_dataset(store)
    assert dataset.pairs[0].source == "critique_guided_incomplete"
    assert "_incomplete" in dataset.pairs[0].source


def test_sample_source_is_human_review_h9() -> None:
    """C2-H9: PreferenceSample.source == 'human_review'."""
    store = _make_store()
    _save_item(store, item_id="src_samp", status="rejected")
    dataset = export_preference_dataset(store)
    assert dataset.samples[0].source == "human_review"


# ── H10: no LLM calls ─────────────────────────────────────────────────────────


def test_export_no_llm_calls_h10(monkeypatch) -> None:
    """C2-H10: export_preference_dataset makes no external HTTP calls."""
    import socket

    def mock_connect(*args, **kwargs):
        raise AssertionError("Unexpected network call in export_preference_dataset")

    monkeypatch.setattr(socket.socket, "connect", mock_connect)
    store = _make_store()
    _save_item(store, item_id="no_llm", trail=_valid_trail())
    _save_item(store, item_id="no_llm2", status="accepted")
    # Should not raise
    dataset = export_preference_dataset(store)
    assert len(dataset.pairs) == 1
    assert len(dataset.samples) == 1


# ── JSONL file output ─────────────────────────────────────────────────────────


def test_export_writes_jsonl_files(tmp_path: Path) -> None:
    """export_preference_dataset with output_dir writes 3 files."""
    store = _make_store()
    _save_item(store, item_id="file_pair", trail=_valid_trail())
    _save_item(store, item_id="file_samp", status="accepted")
    export_preference_dataset(store, output_dir=tmp_path)

    pairs_file = tmp_path / "critique_guided_pairs.jsonl"
    samples_file = tmp_path / "human_signal_samples.jsonl"
    meta_file = tmp_path / "metadata.json"

    assert pairs_file.exists()
    assert samples_file.exists()
    assert meta_file.exists()


def test_export_pairs_jsonl_schema(tmp_path: Path) -> None:
    """Each line in critique_guided_pairs.jsonl has required schema fields."""
    store = _make_store()
    _save_item(store, item_id="schema_pair", trail=_valid_trail())
    export_preference_dataset(store, output_dir=tmp_path)

    lines = (
        (tmp_path / "critique_guided_pairs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(lines) == 1
    row = json.loads(lines[0])

    required_fields = {
        "pair_id", "item_type", "object_ref", "chosen_payload",
        "chosen_score", "rejected_score", "rejected_fixes",
        "trail_length", "source", "confidence", "data_note",
    }
    for field in required_fields:
        assert field in row, f"Missing field: {field}"
    assert row["source"] == "critique_guided_incomplete"
    assert row["confidence"] == 0.5
    assert "rejected artifact unrecoverable" in row["data_note"]
    assert row["trail_length"] >= 2


def test_export_samples_jsonl_schema(tmp_path: Path) -> None:
    """Each line in human_signal_samples.jsonl has required schema fields."""
    store = _make_store()
    _save_item(store, item_id="schema_samp", status="rejected")
    export_preference_dataset(store, output_dir=tmp_path)

    lines = (
        (tmp_path / "human_signal_samples.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(lines) == 1
    row = json.loads(lines[0])

    required_fields = {
        "sample_id", "item_type", "object_ref", "payload",
        "label", "source", "confidence", "data_note",
    }
    for field in required_fields:
        assert field in row, f"Missing field: {field}"
    assert row["source"] == "human_review"
    assert row["confidence"] == 0.9
    assert "no paired" in row["data_note"]
    assert row["label"] == "rejected"


def test_export_metadata_json_schema(tmp_path: Path) -> None:
    """metadata.json has required fields and correct values."""
    store = _make_store()
    _save_item(store, item_id="meta_pair", trail=_valid_trail())
    _save_item(store, item_id="meta_samp", status="accepted")
    export_preference_dataset(store, output_dir=tmp_path)

    meta = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert "generated_at" in meta
    assert meta["total_pairs"] == 1
    assert meta["total_samples"] == 1
    assert "WEAK supervision" in meta["data_quality_note"]
    assert meta["warn_empty"] is False


def test_metadata_warn_empty_true_when_no_data(tmp_path: Path) -> None:
    """metadata.warn_empty=True when 0 pairs and 0 samples."""
    store = _make_store()
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        export_preference_dataset(store, output_dir=tmp_path)
    meta = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert meta["warn_empty"] is True
    assert meta["total_pairs"] == 0
    assert meta["total_samples"] == 0


# ── Mixed store: all filter conditions together ───────────────────────────────


def test_export_mixed_store() -> None:
    """Multiple items with different conditions — correct filtering throughout."""
    store = _make_store()

    # Should be included as pair (trail_length=2, revise→pass)
    _save_item(store, item_id="good_pair", trail=_valid_trail())

    # Should be excluded from pair (trail_length=1)
    _save_item(store, item_id="short_trail", trail=[
        {"round": 0, "verdict": "pass", "score": 0.9, "fixes": [], "reflection": ""},
    ])

    # Should be excluded from pair (no revise step)
    _save_item(store, item_id="all_pass", trail=[
        {"round": 0, "verdict": "pass", "score": 0.8, "fixes": [], "reflection": ""},
        {"round": 1, "verdict": "pass", "score": 0.9, "fixes": [], "reflection": ""},
    ])

    # Should be in samples (accepted)
    _save_item(store, item_id="accepted_item", status="accepted")

    # Should be in samples (rejected)
    _save_item(store, item_id="rejected_item", status="rejected")

    # Should NOT be in samples (pending_review)
    _save_item(store, item_id="pending_item", status="pending_review")

    dataset = export_preference_dataset(store)

    assert len(dataset.pairs) == 1
    assert dataset.pairs[0].pair_id == "good_pair_pair"

    sample_ids = {s.sample_id for s in dataset.samples}
    assert "accepted_item_sample" in sample_ids
    assert "rejected_item_sample" in sample_ids
    assert "pending_item_sample" not in sample_ids
    assert len(dataset.samples) == 2

    labels = {s.label for s in dataset.samples}
    assert "chosen" in labels
    assert "rejected" in labels


# ── No exaggerated vocabulary in default field values ─────────────────────────


def test_no_exaggerated_vocabulary_in_data_note() -> None:
    """Verify default data_note and data_quality_note do not contain banned phrases."""
    pair = PreferencePair(
        pair_id="x",
        item_type="bark_variant",
        object_ref="ref",
        chosen_payload={},
        chosen_score=0.9,
        rejected_score=0.5,
        trail_length=2,
    )
    sample = PreferenceSample(
        sample_id="y",
        item_type="bark_variant",
        object_ref="ref",
        payload={},
        label="chosen",
    )
    meta = DatasetMetadata()

    banned = [
        "training-ready",
        "DPO training dataset",
        "human preference pair",
        "训练就绪",
    ]
    combined = pair.data_note + sample.data_note + meta.data_quality_note
    for phrase in banned:
        assert phrase not in combined, f"Banned phrase found: {phrase!r}"
