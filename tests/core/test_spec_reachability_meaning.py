"""A grounded EF state can be reachable even when its continuation cannot finish."""

import pytest

from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Effect,
    LogicVar,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
)
from owcopilot.logic.temporal import SpecStatus, check_soft_lock, check_temporal_spec
from owcopilot.spec_authoring import OfflineSpecTranslator, OutcomeStatus, translate_intent


@pytest.mark.parametrize("blocked", [False, True])
def test_reachability_backtranslation_matches_real_state_property_not_completion(blocked):
    quest = Quest(
        id="mail",
        title="Receive a letter",
        stages=[
            QuestStage(id="receive", summary="Receive"),
            QuestStage(id="finish", summary="End"),
        ],
        logic=QuestLogic(
            variables=[LogicVar(id="received", name="信件已收到", default=False)],
            stage_logic=[
                StageLogic(stage_id="receive", effects_on_complete=[Effect(var="received")]),
                StageLogic(stage_id="finish", precondition="not received" if blocked else ""),
            ],
            branches=[
                Branch(id="continue", from_stage="receive", to_stage="finish"),
                Branch(id="end", from_stage="finish", outcome="finished"),
            ],
        ),
    )
    translated = translate_intent(
        "「信件已收到」应当可达",
        quest,
        ContentBundle(quests={quest.id: quest}),
        OfflineSpecTranslator(),
    )
    assert translated.status is OutcomeStatus.NEEDS_CONFIRMATION
    assert translated.spec is not None
    assert translated.spec.pattern.value == "EF"
    assert check_temporal_spec(quest, translated.spec).status is SpecStatus.PROVEN
    completion = check_soft_lock(quest)
    assert completion.status is (SpecStatus.VIOLATED if blocked else SpecStatus.PROVEN)
    assert "可达状态" in translated.back_translation
    assert "存在一条通关路径" not in translated.back_translation
