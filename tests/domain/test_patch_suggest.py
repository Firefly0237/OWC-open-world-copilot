"""Tests for the patch suggest service: deterministic fixers, LLM candidates, shadow gating."""

from __future__ import annotations

import json

import pytest

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import (
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    QuestStage,
    Relation,
    Term,
)
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, LLMProvider
from owcopilot.llm.router import StaticRouter
from owcopilot.patches import PatchSuggestService, deterministic_candidates, parse_patch_candidates
from owcopilot.patches.search import applicable_repair_moves, plan_repairs
from owcopilot.patches.suggest import _target_object


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


class _RecordingProvider:
    """Like _ScriptedProvider but also records every `user` prompt string it is asked to serve."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.users: list[str] = []
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls += 1
        self.users.append(user)
        return self.payload, 10, 10


def _gateway_with_provider(provider: LLMProvider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"patch_suggest": "cheap"}),
        cache=NoOpCache(),
    )


# A valid LLM fix for the _bundle() UNKNOWN_ENTITY_REF issue: point the quest at the existing scout
# (distinct from the deterministic fix, which simply removes the dangling giver_npc).
_LLM_REPLACE_PAYLOAD = json.dumps(
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
_LLM_REPLACE_OP = {"op": "replace", "path": "/quests/quest_patrol/giver_npc", "value": "npc_mara"}
_DET_REMOVE_OP = {"op": "remove", "path": "/quests/quest_patrol/giver_npc", "value": None}


def _op_dumps(candidate_or_move) -> list[dict]:
    return [op.model_dump(mode="json") for op in candidate_or_move.ops]


def test_deterministic_fixer_unknown_entity_ref() -> None:
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    candidates = deterministic_candidates(issue, bundle)
    assert candidates
    op = candidates[0].ops[0]
    assert op.op.value == "remove"
    assert op.path == "/quests/quest_patrol/giver_npc"


def test_deterministic_fixer_stage_required_entity() -> None:
    bundle = _bundle()
    bundle.quests["quest_patrol"].giver_npc = "npc_mara"
    bundle.quests["quest_patrol"].stages = [
        QuestStage(
            id="s1",
            summary="Check the border.",
            required_entities=["npc_mara", "npc_ghost"],
        )
    ]
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    candidates = deterministic_candidates(issue, bundle)

    assert candidates
    op = candidates[0].ops[0]
    assert op.op.value == "replace"
    assert op.path == "/quests/quest_patrol/stages/0/required_entities"
    assert op.value == ["npc_mara"]


def test_deterministic_fixer_missing_prerequisite() -> None:
    bundle = ContentBundle(
        quests={
            "quest_next": Quest(
                id="quest_next",
                title="Next",
                objective="Continue the plot.",
                prerequisites=["quest_missing"],
                localization_keys=["quest.quest_next.objective"],
            )
        }
    )
    issue = _issue_for(bundle, "PREREQ_MISSING")
    result = PatchSuggestService(bundle=bundle, audit_runner=_runner()).suggest(issue)

    assert result.candidates
    op = result.candidates[0].candidate.ops[0]
    assert op.path == "/quests/quest_next/prerequisites"
    assert op.value == []


def test_deterministic_fixer_missing_dialogue_ref() -> None:
    bundle = ContentBundle(
        quests={
            "quest_next": Quest(
                id="quest_next",
                title="Next",
                objective="Continue the plot.",
                dialogue_refs=["dlg_missing"],
                localization_keys=["quest.quest_next.objective"],
            )
        }
    )
    issue = _issue_for(bundle, "MISSING_DIALOGUE_REF")
    result = PatchSuggestService(bundle=bundle, audit_runner=_runner()).suggest(issue)

    assert result.candidates
    op = result.candidates[0].candidate.ops[0]
    assert op.path == "/quests/quest_next/dialogue_refs"
    assert op.value == []


def test_deterministic_fixer_missing_relation_endpoint() -> None:
    bundle = ContentBundle(
        entities={
            "npc_mara": Entity(id="npc_mara", name="Mara", type=EntityType.NPC),
        },
        relations=[Relation(source="npc_mara", target="npc_ghost", kind="knows")],
    )
    issue = _issue_for(bundle, "MISSING_RELATION_ENDPOINT")
    result = PatchSuggestService(bundle=bundle, audit_runner=_runner()).suggest(issue)

    assert result.candidates
    op = result.candidates[0].candidate.ops[0]
    assert op.op.value == "remove"
    assert op.path == "/relations/0"


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
    service = PatchSuggestService(bundle=bundle, audit_runner=_runner(), gateway=_gateway(payload))
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
    service = PatchSuggestService(bundle=bundle, audit_runner=_runner(), gateway=_gateway(payload))
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


# --------------------------------------------------------------------------------------------------
# as_candidate_provider — the LLM proposer wired into the MCTS move set (LATS, arXiv:2310.04406).
# --------------------------------------------------------------------------------------------------


def test_provider_threads_call_time_bundle_not_self_bundle() -> None:
    # The proposer prompt must be built from the bundle passed at CALL time (the MCTS node's state),
    # not the construction-time self.bundle — otherwise every node would be scored against the root.
    recorder = _RecordingProvider(_LLM_REPLACE_PAYLOAD)
    a = _bundle()
    issue = _issue_for(a, "UNKNOWN_ENTITY_REF")
    b = a.model_copy(deep=True)
    b.quests["quest_patrol"].objective = "A completely different objective."  # mutate target object

    service = PatchSuggestService(
        bundle=a, audit_runner=_runner(), gateway=_gateway_with_provider(recorder)
    )
    provider = service.as_candidate_provider(max_llm_calls=10)
    provider(issue, a)
    provider(issue, b)  # distinct (issue, state) key -> a second, distinct model call

    assert len(recorder.users) == 2
    assert json.loads(recorder.users[1])["target_object"] == _target_object(b, issue.target_ref)
    assert json.loads(recorder.users[1])["target_object"] != _target_object(a, issue.target_ref)


def test_llm_candidate_enters_validated_move_set() -> None:
    bundle = _bundle()
    runner = _runner()
    service = PatchSuggestService(
        bundle=bundle, audit_runner=runner, gateway=_gateway(_LLM_REPLACE_PAYLOAD)
    )
    provider = service.as_candidate_provider(max_llm_calls=10)

    moves = applicable_repair_moves(bundle, runner, candidate_provider=provider)
    move_ops = [_op_dumps(move) for move, _child, _errors in moves]
    assert [_LLM_REPLACE_OP] in move_ops  # the model fix survived shadow-validation as a move
    assert [_DET_REMOVE_OP] in move_ops  # the deterministic fixer is still there too (widened set)

    plan = plan_repairs(bundle, runner, candidate_provider=provider, seed=0)
    assert plan.final_open_errors == 0  # either validated move clears the target error


def test_llm_candidate_that_worsens_is_pruned() -> None:
    # An LLM op that blanks the objective introduces QUEST_MISSING_OBJECTIVE. The shared move
    # generator's non-worsening prune must drop it — no new safety code on the model path.
    worsening = json.dumps(
        {
            "candidates": [
                {
                    "ops": [
                        {"op": "replace", "path": "/quests/quest_patrol/objective", "value": ""}
                    ],
                    "rationale": "blank the objective",
                }
            ]
        }
    )
    bundle = _bundle()
    runner = _runner()
    service = PatchSuggestService(bundle=bundle, audit_runner=runner, gateway=_gateway(worsening))
    provider = service.as_candidate_provider(max_llm_calls=10)

    moves = applicable_repair_moves(bundle, runner, candidate_provider=provider)
    for move, _child, _errors in moves:
        assert "/quests/quest_patrol/objective" not in [op.path for op in move.ops]


def test_max_llm_calls_hard_cap_falls_back_marked() -> None:
    scripted = _ScriptedProvider(_LLM_REPLACE_PAYLOAD)
    a = _bundle()
    issue = _issue_for(a, "UNKNOWN_ENTITY_REF")
    b = a.model_copy(deep=True)
    b.quests["quest_patrol"].objective = "A completely different objective."

    service = PatchSuggestService(
        bundle=a, audit_runner=_runner(), gateway=_gateway_with_provider(scripted)
    )
    provider = service.as_candidate_provider(max_llm_calls=1)

    first = provider(issue, a)  # spends the one allowed model call
    second = provider(issue, b)  # budget exhausted -> deterministic fallback, marked

    assert scripted.calls == 1  # the hard cap held; the second state made no model call
    # First result: unmarked, and includes the model-proposed candidate.
    assert [_LLM_REPLACE_OP] in [_op_dumps(c) for c in first]
    assert all("budget-exhausted" not in c.rationale for c in first)
    # Second result: exactly the deterministic candidates (ops), every rationale marked.
    assert [_op_dumps(c) for c in second] == [
        _op_dumps(c) for c in deterministic_candidates(issue, b)
    ]
    assert second and all("budget-exhausted" in c.rationale for c in second)


def test_provider_caches_per_issue_and_state() -> None:
    scripted = _ScriptedProvider(_LLM_REPLACE_PAYLOAD)
    bundle = _bundle()
    issue = _issue_for(bundle, "UNKNOWN_ENTITY_REF")
    service = PatchSuggestService(
        bundle=bundle, audit_runner=_runner(), gateway=_gateway_with_provider(scripted)
    )
    provider = service.as_candidate_provider(max_llm_calls=10)

    first = provider(issue, bundle)
    second = provider(issue, bundle)  # same (issue, state) -> cache hit, no second model call

    assert scripted.calls == 1
    assert [_op_dumps(c) for c in first] == [_op_dumps(c) for c in second]


def test_as_candidate_provider_requires_gateway() -> None:
    service = PatchSuggestService(bundle=_bundle(), audit_runner=_runner(), gateway=None)
    with pytest.raises(ValueError, match="gateway"):
        service.as_candidate_provider(max_llm_calls=1)


def test_plan_repairs_default_provider_is_byte_identical() -> None:
    # The opt-in provider default must be a pure no-op: the implicit default and an explicit
    # deterministic_candidates provider must produce byte-identical plans.
    bundle = _bundle()
    runner = _runner()
    implicit = plan_repairs(bundle, runner, seed=0)
    explicit = plan_repairs(bundle, runner, seed=0, candidate_provider=deterministic_candidates)
    assert implicit.model_dump() == explicit.model_dump()
