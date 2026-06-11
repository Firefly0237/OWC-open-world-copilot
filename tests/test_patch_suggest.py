"""Tests for the patch suggest service: deterministic fixers, LLM candidates, shadow gating."""

from __future__ import annotations

import json

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation, Term
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.patches import PatchSuggestService, deterministic_candidates, parse_patch_candidates


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara",
                name="Mara",
                type=EntityType.NPC,
                description="Border scout.",
            ),
            "loc_fort": Entity(
                id="loc_fort",
                name="Border Fort",
                type=EntityType.LOCATION,
                description="A fort.",
            ),
        },
        relations=[
            Relation(source="npc_mara", target="loc_fort", kind="located_in"),
        ],
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="Patrol the Border",
                giver_npc="npc_ghost",  # unknown entity -> UNKNOWN_ENTITY_REF
                location="loc_fort",
                objective="Walk the border line.",
                localization_keys=["quest.quest_patrol.objective"],
            )
        },
    )


def _runner() -> AuditRunner:
    return AuditRunner(build_default_rule_registry())


def _issue_for(bundle: ContentBundle, rule_code: str):
    result = _runner().run(AuditContext.from_bundle(bundle))
    matches = [issue for issue in result.issues if issue.rule_code == rule_code]
    assert matches, f"expected {rule_code} in audit result"
    return matches[0]


class _ScriptedProvider:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls += 1
        return self.payload, 10, 10


def _gateway(payload: str) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": _ScriptedProvider(payload)},
        router=StaticRouter(mapping={"patch_suggest": "cheap"}),
        cache=NoOpCache(),
    )


def test_deterministic_fixer_unknown_entity_ref() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    candidates = deterministic_candidates(issue, bundle)
    assert candidates
    op = candidates[0].ops[0]
    assert op.op.value == "remove"
    assert op.path == "/quests/quest_patrol/giver_npc"


def test_suggest_offline_resolves_target_issue() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    service = PatchSuggestService(bundle=bundle, audit_runner=_runner())
    result = service.suggest(issue)
    assert not result.used_llm
    assert result.candidates, "deterministic candidate should survive shadow validation"
    top = result.candidates[0]
    assert top.target_resolved
    assert top.source == "deterministic"
    assert top.candidate.id and top.candidate.id.startswith("patch_")
    assert top.candidate.issue_id


def test_suggest_llm_candidate_ranked_and_validated() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    payload = json.dumps(
        {
            "candidates": [
                {
                    "ops": [
                        {
                            "op": "replace",
                            "path": "/quests/quest_patrol/giver_npc",
                            "value": "npc_mara",
                        }
                    ],
                    "rationale": "Point the quest at the existing scout.",
                }
            ]
        }
    )
    service = PatchSuggestService(
        bundle=bundle, audit_runner=_runner(), gateway=_gateway(payload)
    )
    result = service.suggest(issue)
    assert result.used_llm
    assert not result.parse_failed
    sources = {item.source for item in result.candidates}
    assert "llm" in sources and "deterministic" in sources
    assert all(item.target_resolved for item in result.candidates)


def test_suggest_drops_candidate_that_introduces_new_error() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    # This "fix" empties the objective -> introduces QUEST_MISSING_OBJECTIVE.
    payload = json.dumps(
        {
            "candidates": [
                {
                    "ops": [
                        {"op": "remove", "path": "/quests/quest_patrol/giver_npc"},
                        {"op": "replace", "path": "/quests/quest_patrol/objective", "value": ""},
                    ]
                }
            ]
        }
    )
    service = PatchSuggestService(
        bundle=bundle, audit_runner=_runner(), gateway=_gateway(payload)
    )
    result = service.suggest(issue)
    assert result.rejected_count >= 1
    for item in result.candidates:  # the bad combo op never surfaces
        paths = [op.path for op in item.candidate.ops]
        assert "/quests/quest_patrol/objective" not in paths


def test_suggest_survives_unparseable_llm_output() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    service = PatchSuggestService(
        bundle=bundle, audit_runner=_runner(), gateway=_gateway("I would just delete the field.")
    )
    result = service.suggest(issue)
    assert result.parse_failed
    assert result.candidates, "deterministic fallback still present"


def test_parser_tolerates_real_model_shapes() -> None:
    raw = json.dumps(
        {
            "patches": [
                {
                    "ops": [
                        {"op": "set", "path": "quests/quest_patrol/giver_npc", "value": "npc_mara"},
                        {"op": "DELETE", "path": "quests.quest_patrol.location"},
                    ]
                }
            ]
        }
    )
    candidates = parse_patch_candidates(raw)
    assert candidates[0].ops[0].op.value == "replace"
    assert candidates[0].ops[0].path == "/quests/quest_patrol/giver_npc"
    assert candidates[0].ops[1].op.value == "remove"
    assert candidates[0].ops[1].path == "/quests/quest_patrol/location"


def test_term_fixer_replaces_forbidden_term() -> None:
    bundle = _bundle()
    bundle.terms["term_fort"] = Term(
        id="term_fort", canonical="Border Fort", forbidden=["old fort"]
    )
    bundle.quests["quest_patrol"].objective = "Walk to the Old Fort gate."
    issue = _issue_for(bundle, "TERM_INCONSISTENT")
    service = PatchSuggestService(bundle=bundle, audit_runner=_runner())
    result = service.suggest(issue)
    assert result.candidates
    op = result.candidates[0].candidate.ops[0]
    assert op.path == "/quests/quest_patrol/objective"
    assert "Border Fort" in str(op.value)
