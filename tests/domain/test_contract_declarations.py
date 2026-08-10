from __future__ import annotations

import importlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "contracts" / "n1_1_contract_registry.json"


CORE_IMPLEMENTED = {
    "ArtifactIdentity": ("owcopilot.contracts.identity", "ArtifactIdentity"),
    "ContentObjectVersion": ("owcopilot.contracts.revisions", "ContentObjectVersion"),
    "ContentRevision": ("owcopilot.contracts.revisions", "ContentRevision"),
    "OwnershipScope": ("owcopilot.contracts.identity", "OwnershipScope"),
    "ReleasePointer": ("owcopilot.contracts.revisions", "ReleasePointer"),
    "RevisionMembership": ("owcopilot.contracts.revisions", "RevisionMembership"),
}
N21_IMPLEMENTED = {
    "IntakeRun": ("owcopilot.intake.contracts", "IntakeRun"),
    "SourceSegment": ("owcopilot.intake.contracts", "SourceSegment"),
    "TriageVerdict": ("owcopilot.intake.contracts", "TriageVerdict"),
}
IMPLEMENTED = CORE_IMPLEMENTED | N21_IMPLEMENTED

DECLARED_BY_NODE = {
    "N2.2": {
        "ChangeSet": "owcopilot.authoring.contracts",
        "ReviewDecision": "owcopilot.authoring.contracts",
    },
    "N3.1": {
        "EvidenceUnit": "owcopilot.knowledge.contracts",
        "DerivedClaim": "owcopilot.knowledge.contracts",
    },
    "N4.1": {
        "ExecutionScope": "owcopilot.agent.contracts",
        "TaskContract": "owcopilot.agent.contracts",
        "PlanArtifact": "owcopilot.agent.contracts",
        "EffectSpec": "owcopilot.agent.contracts",
        "ApprovalGrant": "owcopilot.agent.contracts",
        "EvidenceRecord": "owcopilot.agent.contracts",
        "VerifierVerdict": "owcopilot.agent.contracts",
    },
    "N4.3": {
        "MemoryItem": "owcopilot.memory.contracts",
        "MemoryEvent": "owcopilot.memory.contracts",
        "RetrievalPolicyVersion": "owcopilot.memory.contracts",
    },
}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_registry() -> dict[str, Any]:
    return json.loads(
        REGISTRY_PATH.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )


def _records_by_name() -> dict[str, dict[str, Any]]:
    records = _load_registry()["contracts"]
    return {record["name"]: record for record in records}


def _module_has_symbol(module_name: str, symbol_name: str) -> bool:
    try:
        if importlib.util.find_spec(module_name) is None:
            return False
    except (ModuleNotFoundError, ValueError):
        return False
    return hasattr(importlib.import_module(module_name), symbol_name)


def test_registry_has_exact_implemented_and_declared_contract_sets() -> None:
    registry = _load_registry()
    records = registry["contracts"]
    flattened_declared = {
        name: module
        for contracts in DECLARED_BY_NODE.values()
        for name, module in contracts.items()
    }

    assert registry["schema_version"] == 2
    assert registry["node"] == "N2.1"
    assert len(records) == 23
    assert Counter(record["status"] for record in records) == {
        "IMPLEMENTED": 9,
        "DECLARED": 14,
    }
    assert {record["name"] for record in records} == set(IMPLEMENTED) | set(flattened_declared)
    assert len({record["name"] for record in records}) == len(records)

    boundary = registry["capability_boundary"]
    assert boundary == {
        "declared_counts_as_implemented": False,
        "c13_claim_tier": "NONE",
        "c13_state": "NOT_AVAILABLE",
    }


def test_every_contract_has_one_planned_first_consumer_and_acceptance_node() -> None:
    common_keys = {
        "name",
        "status",
        "first_consumer_node",
        "first_consumer_component",
        "acceptance_node",
        "legacy_name_collisions",
    }

    for record in _load_registry()["contracts"]:
        assert isinstance(record["first_consumer_node"], str) and record["first_consumer_node"]
        assert isinstance(record["first_consumer_component"], str)
        assert record["first_consumer_component"].strip() == record["first_consumer_component"]
        assert record["first_consumer_component"]
        assert isinstance(record["acceptance_node"], str) and record["acceptance_node"]
        assert isinstance(record["legacy_name_collisions"], list)

        if record["status"] == "IMPLEMENTED":
            assert set(record) == common_keys | {"implementation"}
            assert record["acceptance_node"] in {"N1.1", "N2.1"}
            if record["acceptance_node"] == "N2.1":
                assert record["first_consumer_node"] == "N2.1"
        else:
            assert record["status"] == "DECLARED"
            assert set(record) == common_keys | {"planned_module", "planned_symbol"}
            assert record["acceptance_node"] == record["first_consumer_node"]
            assert record["first_consumer_node"] != "N1.1"


def test_implemented_entries_resolve_to_the_actual_defining_symbol() -> None:
    records = _records_by_name()

    for name, (module_name, symbol_name) in IMPLEMENTED.items():
        record = records[name]
        assert record["status"] == "IMPLEMENTED"
        assert record["implementation"] == {"module": module_name, "symbol": symbol_name}

        module = importlib.import_module(module_name)
        symbol = getattr(module, symbol_name)
        assert symbol.__name__ == name
        assert symbol.__module__ == module_name


def test_core_package_keeps_n11_exports_and_n21_uses_its_owned_package() -> None:
    contracts_package = importlib.import_module("owcopilot.contracts")

    assert set(contracts_package.__all__) == set(CORE_IMPLEMENTED)
    for name, (module_name, symbol_name) in CORE_IMPLEMENTED.items():
        defining_module = importlib.import_module(module_name)
        assert getattr(contracts_package, name) is getattr(defining_module, symbol_name)
    intake_package = importlib.import_module("owcopilot.intake")
    for name, (module_name, symbol_name) in N21_IMPLEMENTED.items():
        defining_module = importlib.import_module(module_name)
        assert getattr(intake_package, name) is getattr(defining_module, symbol_name)
        assert not hasattr(contracts_package, name)


def test_declared_entries_have_exact_future_ownership_but_no_implementation() -> None:
    records = _records_by_name()
    contracts_package = importlib.import_module("owcopilot.contracts")

    for acceptance_node, expected in DECLARED_BY_NODE.items():
        for name, planned_module in expected.items():
            record = records[name]
            assert record["status"] == "DECLARED"
            assert record["planned_module"] == planned_module
            assert record["planned_symbol"] == name
            assert record["first_consumer_node"] == acceptance_node
            assert record["acceptance_node"] == acceptance_node
            assert "implementation" not in record
            assert not _module_has_symbol(planned_module, name), (
                f"{planned_module}.{name} was implemented before its node"
            )
            assert not hasattr(contracts_package, name), f"DECLARED contract {name} was re-exported"


def test_legacy_name_collisions_are_explicit_and_cannot_alias() -> None:
    registry = _load_registry()
    records = _records_by_name()

    assert registry["alias_policy"] == "FORBID_ALIAS_OR_REEXPORT"
    expected = {
        "ChangeSet": "owcopilot.impact.models.ChangeSet",
        "ReviewDecision": "owcopilot.pipeline.review.ReviewDecision",
    }
    for name, qualified_name in expected.items():
        assert records[name]["legacy_name_collisions"] == [
            {"qualified_name": qualified_name, "may_alias": False}
        ]

    for name, record in records.items():
        if name not in expected:
            assert record["legacy_name_collisions"] == []
        for collision in record["legacy_name_collisions"]:
            assert collision["may_alias"] is False
            assert collision["qualified_name"] != record.get("planned_module")
