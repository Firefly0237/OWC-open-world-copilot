from __future__ import annotations

from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.patches.models import PatchCandidate, PatchOp, PatchOperation
from owcopilot.patches.validate import valid_patch_candidates, validate_patch_candidate


def test_validate_patch_candidate_reports_resolved_errors() -> None:
    bundle = ContentBundle(quests={"q1": Quest(id="q1", title="Q1")})
    candidate = PatchCandidate(
        ops=[
            PatchOperation(op=PatchOp.REPLACE, path="/quests/q1/objective", value="Find Aldric"),
            PatchOperation(
                op=PatchOp.ADD,
                path="/quests/q1/localization_keys/-",
                value="quest.q1.objective",
            ),
        ]
    )
    runner = AuditRunner(build_default_rule_registry())

    validation = validate_patch_candidate(bundle, candidate, runner)

    assert validation.valid
    assert validation.resolved_errors
    assert validation.introduced_errors == []


def test_valid_patch_candidates_filters_candidates_that_introduce_errors() -> None:
    bundle = ContentBundle(
        entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)},
        quests={
            "q1": Quest(
                id="q1",
                title="Q1",
                objective="Find Aldric",
                localization_keys=["quest.q1.objective"],
            )
        },
    )
    invalid = PatchCandidate(
        ops=[PatchOperation(op=PatchOp.REPLACE, path="/quests/q1/giver_npc", value="npc_missing")]
    )
    valid = PatchCandidate(
        ops=[PatchOperation(op=PatchOp.REPLACE, path="/quests/q1/giver_npc", value="npc_aldric")]
    )

    validations = valid_patch_candidates(
        bundle,
        [invalid, valid],
        AuditRunner(build_default_rule_registry()),
    )

    assert [validation.candidate for validation in validations] == [valid]
