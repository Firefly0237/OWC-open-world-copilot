# 规约翻译 agent 的专属测试：确定性编排 + LLM 单节点(fake) + 闭世界接地 + 回译 + 端到端回环。
"""Spec-authoring agent tests — the NL→spec pipeline runs end-to-end at $0 (offline translator) and
with a fake gateway model (real path), and the deterministic steps (grounding, assembly, back-
translation) decide every truth, never the translator.
"""

from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.logic.temporal import PropertyPattern
from owcopilot.spec_authoring import (
    DraftPattern,
    GatewaySpecTranslator,
    OfflineSpecTranslator,
    OperandStatus,
    OutcomeStatus,
    ScriptedSpecTranslator,
    SpecDraft,
    build_symbol_catalog,
    ground_surface,
    translate_intent,
)
from owcopilot.spec_authoring.pipeline import draft_to_json


def _vault_quest(set_both: bool = False) -> Quest:
    effects = [Effect(var="key_obtained", op="set", value=True)]
    if set_both:
        effects.append(Effect(var="vault_open", op="set", value=True))
    return Quest(
        id="q_vault", title="金库",
        stages=[QuestStage(id="s0", summary=""), QuestStage(id="s1", summary="")],
        logic=QuestLogic(
            variables=[
                LogicVar(id="key_obtained", name="密钥已获取", type=LogicVarType.BOOL),
                LogicVar(id="vault_open", name="金库已开启", type=LogicVarType.BOOL),
                LogicVar(id="progress", name="进度", type=LogicVarType.INT, default=0),
            ],
            stage_logic=[StageLogic(stage_id="s0", effects_on_complete=effects)],
            branches=[
                Branch(id="adv", from_stage="s0", to_stage="s1"),
                Branch(id="fin", from_stage="s1", outcome="done"),
            ],
        ),
    )


def _bundle(quest: Quest) -> ContentBundle:
    bundle = ContentBundle()
    bundle.quests[quest.id] = quest
    bundle.entities["baoyu"] = Entity(id="baoyu", name="宝玉", type=EntityType.NPC)
    return bundle


_OFFLINE = OfflineSpecTranslator()


# --------------------------------------------------------------------------------------------------
# Closed-world grounding (the deterministic wall against hallucinated flags).
# --------------------------------------------------------------------------------------------------


def test_grounding_resolves_flag_by_name_and_id() -> None:
    catalog = build_symbol_catalog(_vault_quest(), _bundle(_vault_quest()))
    by_name = ground_surface("密钥已获取", catalog)
    assert by_name.status is OperandStatus.GROUNDED and by_name.flag_id == "key_obtained"
    by_id = ground_surface("vault_open", catalog)
    assert by_id.status is OperandStatus.GROUNDED and by_id.flag_id == "vault_open"


def test_grounding_rejects_entity_without_a_flag() -> None:
    catalog = build_symbol_catalog(_vault_quest(), _bundle(_vault_quest()))
    result = ground_surface("宝玉", catalog)
    assert result.status is OperandStatus.ENTITY_NO_FLAG
    assert "baoyu" in result.note


def test_grounding_rejects_unknown_symbol() -> None:
    catalog = build_symbol_catalog(_vault_quest(), _bundle(_vault_quest()))
    assert ground_surface("不存在的旗标", catalog).status is OperandStatus.UNKNOWN


# --------------------------------------------------------------------------------------------------
# End-to-end pipeline with the offline ($0) translator.
# --------------------------------------------------------------------------------------------------


def test_mutex_intent_produces_a_grounded_candidate_and_back_translation() -> None:
    quest = _vault_quest()
    outcome = translate_intent(
        "「密钥已获取」和「金库已开启」不该同时成立", quest, _bundle(quest), _OFFLINE
    )
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert outcome.spec is not None
    assert outcome.spec.pattern is PropertyPattern.AG
    assert outcome.spec.expression == "not (key_obtained and vault_open)"
    assert "密钥已获取" in outcome.back_translation and "金库已开启" in outcome.back_translation
    assert all(g.status is OperandStatus.GROUNDED for g in outcome.groundings)


def test_reachable_intent_maps_to_ef() -> None:
    quest = _vault_quest()
    outcome = translate_intent("「密钥已获取」应当可达", quest, _bundle(quest), _OFFLINE)
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert outcome.spec is not None and outcome.spec.pattern is PropertyPattern.EF


def test_never_intent_maps_to_ag_negation() -> None:
    quest = _vault_quest()
    outcome = translate_intent("「金库已开启」永不成立", quest, _bundle(quest), _OFFLINE)
    assert outcome.spec is not None and outcome.spec.expression == "not vault_open"


def test_no_softlock_intent_needs_no_operands() -> None:
    quest = _vault_quest()
    outcome = translate_intent(
        "这个任务任何时候都能通关，不会卡死", quest, _bundle(quest), _OFFLINE
    )
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert outcome.spec is not None and outcome.spec.pattern is PropertyPattern.AG_EF_ENDING


def test_entity_surface_is_rejected_by_the_closed_world_gate() -> None:
    quest = _vault_quest()
    outcome = translate_intent("「宝玉」永不「金库已开启」", quest, _bundle(quest), _OFFLINE)
    assert outcome.status is OutcomeStatus.REJECTED
    assert "宝玉" in outcome.message


def test_unknown_surface_is_rejected() -> None:
    quest = _vault_quest()
    outcome = translate_intent("「不存在的旗标」永不成立", quest, _bundle(quest), _OFFLINE)
    assert outcome.status is OutcomeStatus.REJECTED


def test_precedence_intent_is_unsupported_in_a_tier() -> None:
    quest = _vault_quest()
    outcome = translate_intent(
        "拿到「密钥已获取」之前「金库已开启」必须先满足", quest, _bundle(quest), _OFFLINE
    )
    assert outcome.status is OutcomeStatus.UNSUPPORTED


def test_mutex_arity_rejects_a_single_operand() -> None:
    quest = _vault_quest()
    outcome = translate_intent("「密钥已获取」不该同时成立", quest, _bundle(quest), _OFFLINE)
    assert outcome.status is OutcomeStatus.REJECTED
    assert "需要 2 个符号" in outcome.message


def test_type_error_in_assembled_spec_is_rejected() -> None:
    # MUTEX over an int flag → "and needs booleans" type error → deterministic reject, not a crash.
    quest = _vault_quest()
    outcome = translate_intent(
        "「进度」和「密钥已获取」不能同时成立", quest, _bundle(quest), _OFFLINE
    )
    assert outcome.status is OutcomeStatus.REJECTED
    assert "类型检查" in outcome.message


# --------------------------------------------------------------------------------------------------
# The LLM node — real gateway path and scripted path, both at $0.
# --------------------------------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, *, system: str, user: str, model: str):
        return self.reply, 10, 10


def _gateway(reply: str) -> LLMGateway:
    return LLMGateway(providers={"cheap": _FakeProvider(reply)}, router=StaticRouter())


def test_gateway_translator_parses_a_json_draft_from_the_model() -> None:
    draft_json = draft_to_json(
        SpecDraft(
            pattern=DraftPattern.MUTEX, operands=["密钥已获取", "金库已开启"], description="互斥"
        )
    )
    quest = _vault_quest()
    translator = GatewaySpecTranslator(_gateway(draft_json))
    outcome = translate_intent("钥匙和金库互斥", quest, _bundle(quest), translator)
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert outcome.spec is not None
    assert outcome.spec.expression == "not (key_obtained and vault_open)"


def test_gateway_translator_falls_back_on_an_unparseable_reply() -> None:
    quest = _vault_quest()
    # a reply with no JSON → the translator degrades to the offline keyword classifier (no crash).
    translator = GatewaySpecTranslator(_gateway("对不起我不会"))
    outcome = translate_intent(
        "「密钥已获取」和「金库已开启」不该同时成立", quest, _bundle(quest),
        translator,
    )
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert outcome.spec is not None
    assert translator.used_fallback is True
    assert translator.fallback_reason.startswith("invalid_structured_output:")


def test_authored_spec_id_is_stable_but_distinguishes_intent_and_formula() -> None:
    quest = _vault_quest()
    bundle = _bundle(quest)
    first = translate_intent("「密钥已获取」应当可达", quest, bundle, _OFFLINE)
    repeated = translate_intent("「密钥已获取」应当可达", quest, bundle, _OFFLINE)
    reworded = translate_intent("「密钥已获取」能够达成", quest, bundle, _OFFLINE)
    other_formula = translate_intent("「金库已开启」应当可达", quest, bundle, _OFFLINE)
    assert first.spec is not None
    assert repeated.spec is not None
    assert reworded.spec is not None
    assert other_formula.spec is not None
    assert first.spec.id == repeated.spec.id
    assert first.spec.id != reworded.spec.id
    assert first.spec.id != other_formula.spec.id


def test_scripted_translator_drives_a_fixed_draft() -> None:
    quest = _vault_quest()
    translator = ScriptedSpecTranslator(
        SpecDraft(pattern=DraftPattern.REACHABLE, operands=["密钥已获取"])
    )
    outcome = translate_intent("(意图文本无所谓)", quest, _bundle(quest), translator)
    assert outcome.spec is not None and outcome.spec.pattern is PropertyPattern.EF


# --------------------------------------------------------------------------------------------------
# End-to-end round trip — a confirmed candidate, stored on the quest, is caught by the audit rule.
# --------------------------------------------------------------------------------------------------


def test_confirmed_candidate_stored_in_metadata_is_enforced_by_the_audit() -> None:
    quest = _vault_quest(set_both=True)  # this quest VIOLATES the mutex the planner intends
    bundle = _bundle(quest)
    outcome = translate_intent(
        "「密钥已获取」和「金库已开启」不该同时成立", quest, bundle, _OFFLINE
    )
    assert outcome.status is OutcomeStatus.NEEDS_CONFIRMATION and outcome.spec is not None

    # a human confirms the back-translation → the spec is stored on the quest metadata …
    quest.metadata["temporal_specs"] = [outcome.spec.model_dump(mode="json")]
    # … and the deterministic audit now enforces it, catching the violation with a witness.
    result = AuditRunner(build_default_rule_registry()).run(AuditContext.from_bundle(bundle))
    violated = [
        i for i in result.issues
        if i.rule_code == "FLAG_TEMPORAL_INVARIANT" and "AG_INVARIANT_VIOLATED" in i.message
    ]
    assert len(violated) == 1
    assert violated[0].evidence[0].data["witness"] == ["adv"]
