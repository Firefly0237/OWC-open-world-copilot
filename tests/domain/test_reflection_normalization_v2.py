"""Exact representations may normalize; semantic guesses and ambiguous aliases cannot."""

import json
from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.evolution import SourceRef
from owcopilot.workflow.evolution_v2 import ReflectionReport
from owcopilot.workflow.reflection_continuation_v2 import (
    available_parameter_bindings,
    bound_parameter_schema,
    normalize_reflection_report,
)


def test_exact_frozen_slot_normalization_preserves_raw_text_and_rejects_guesses():
    source = SourceRef(kind="development_input", id="source", content_hash=content_hash("snapshot"))
    snapshot = {
        "entities": {"baoyu": {"name": "贾宝玉"}, "daiyu": {"name": "林黛玉"}},
        "machine_requirements": {
            "stage_count": 1,
            "distinct_outcomes": True,
            "game_state_variables": ["reputation", "message_public"],
            "participant_ids": ["baoyu", "daiyu"],
        },
    }
    report = {
        "diagnosis": "Grounded observation",
        "target_problem": "state contract",
        "proposed_change": "mixed",
        "evidence_refs": [source.model_dump(mode="json")],
        "reusable_entries": [
            {
                "id": "typed_state_contract",
                "text": (
                    "For {{actor}}, keep {{count}} stages, "
                    "distinct={{different}}, state={{state}}."
                ),
                "support_refs": [source.model_dump(mode="json")],
                "slots": [
                    {
                        "name": "actor",
                        "kind": "entity",
                        "binding_key": "participant:0",
                        "source_value": "贾宝玉",
                    },
                    {
                        "name": "count",
                        "kind": "requirement",
                        "binding_key": "machine_requirements.stage_count",
                        "source_value": "1",
                    },
                    {
                        "name": "different",
                        "kind": "requirement",
                        "binding_key": "machine_requirements.distinct_outcomes",
                        "source_value": "true",
                    },
                    {
                        "name": "state",
                        "kind": "requirement",
                        "binding_key": "machine_requirements.game_state_variables",
                        "source_value": "reputation,message_public",
                    },
                ],
            }
        ],
    }
    raw = "```json\n" + json.dumps(report, ensure_ascii=False) + "\n```"
    untouched = deepcopy(report)
    receipt = normalize_reflection_report(raw, snapshot, [source])
    assert report == untouched and receipt["raw_text"] == raw
    assert receipt["raw_text_hash"] == content_hash(raw)
    slots = {
        slot["name"]: slot for slot in receipt["normalized_report"]["reusable_entries"][0]["slots"]
    }
    assert slots["actor"]["source_value"] == "baoyu"
    assert slots["count"]["binding_key"] == "stage_count"
    assert slots["different"]["source_value"] == "True"
    assert slots["state"]["source_value"] == str(["reputation", "message_public"])
    assert receipt["grounding"] == "verified" and receipt["semantic_changes"] == "none"
    assert receipt["normalization_hash"] == content_hash(
        {k: v for k, v in receipt.items() if k != "normalization_hash"}
    )
    schema = bound_parameter_schema(ReflectionReport, snapshot)
    assert schema["$defs"]["ParameterSlot"]["oneOf"]
    assert any(row["source_value"] == "baoyu" for row in available_parameter_bindings(snapshot))
    for name, value in (
        ("count", "1.0"),
        ("different", "1"),
        ("actor", "宝玉"),
        ("state", "message_public,reputation"),
    ):
        invalid = deepcopy(report)
        next(slot for slot in invalid["reusable_entries"][0]["slots"] if slot["name"] == name)[
            "source_value"
        ] = value
        with pytest.raises(ValueError):
            normalize_reflection_report(json.dumps(invalid), snapshot, [source])
    ambiguous = deepcopy(snapshot)
    ambiguous["entities"]["other"] = {"name": "贾宝玉"}
    with pytest.raises(ValueError):
        normalize_reflection_report(raw, ambiguous, [source])
    unknown_namespace = deepcopy(report)
    unknown_namespace["reusable_entries"][0]["slots"][1]["binding_key"] = "requirements.stage_count"
    with pytest.raises(ValueError):
        normalize_reflection_report(json.dumps(unknown_namespace), snapshot, [source])
