"""FastAPI contract tests for the read-only/propose-only engineering workbench."""

from __future__ import annotations

import json
from types import SimpleNamespace

from owcopilot.content.models import (  # noqa: E402
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    StageLogic,
    StyleElementKind,
    StyleRubric,
    StyleRubricElement,
)
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.llm.telemetry import CallRecord, TelemetryCollector  # noqa: E402
from owcopilot.storage import SQLiteStore  # noqa: E402


def _world() -> ContentBundle:
    quest = Quest(
        id="q_logic",
        title="旗标之路",
        giver_npc="npc_missing",
        stages=[QuestStage(id="s0", summary="作出选择")],
        logic=QuestLogic(
            variables=[LogicVar(id="shared", type=LogicVarType.BOOL, default=False)],
            stage_logic=[
                StageLogic(
                    stage_id="s0",
                    effects_on_complete=[Effect(var="shared", op="set", value=True)],
                )
            ],
            branches=[
                Branch(
                    id="b_finish",
                    from_stage="s0",
                    condition="shared == true",
                    outcome="completed",
                )
            ],
        ),
    )
    tree = DialogueTree(
        id="dlg_gate",
        title="门前问答",
        root_node="n0",
        nodes={
            "n0": DialogueNode(
                id="n0",
                text="是否继续？",
                choices=[DialogueChoice(text="继续", next_node=None)],
            )
        },
    )
    rubric = StyleRubric(
        id="wafu",
        label="和风清单",
        version="1",
        elements=[
            StyleRubricElement(
                id="katana",
                text="出现日本刀剑器物",
                kind=StyleElementKind.LEXICAL,
                terms=["武士刀"],
            )
        ],
    )
    return ContentBundle(
        entities={
            "npc_samurai": Entity(
                id="npc_samurai",
                name="剑客",
                type=EntityType.NPC,
                description="腰间挂着一把武士刀。",
            )
        },
        quests={quest.id: quest},
        dialogue_trees={tree.id: tree},
        style_rubrics={rubric.id: rubric},
    )


def test_temporal_spec_action_reports_real_model_fallback_honestly(
    tmp_path, monkeypatch
) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "spec-action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()

    class InvalidReplyGateway:
        def complete(self, **_kwargs) -> str:
            telemetry.record(
                CallRecord(
                    task="spec_translate",
                    tier="cheap",
                    input_tokens=8,
                    output_tokens=3,
                )
            )
            return "not structured JSON"

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (InvalidReplyGateway(), telemetry),
    )
    result = engineering_actions.translate_temporal_spec_action(
        root,
        quest_id="q_logic",
        intent="「shared」应当可达",
        use_llm=True,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.01,
    )
    assert result["translator_mode"] == "real_model_fallback"
    assert result["used_llm"] is True
    assert result["degraded"] is True
    assert result["degradation_reasons"][0].startswith("invalid_structured_output:")
    assert result["outcome"]["status"] == "needs_confirmation"
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.01
    assert result["writes_canon"] is False


def _save_run(db_path, run_id: str, created_at: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_path)
    try:
        store.save_agent_run(
            run_id=run_id,
            goal=f"goal-{run_id}",
            stop_reason="finished",
            step_count=1,
            cost_usd=0.0,
            latency_ms=10.0,
            agent_name="diagnoser",
            transcript_json=json.dumps({"steps": [{"action": "audit_project"}]}),
            created_at=created_at,
        )
    finally:
        store.close()


def test_repair_plan_action_reuses_model_candidate_provider_and_reports_actual_calls(
    tmp_path, monkeypatch
) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()

    class FakeGateway:
        def complete(self, **_kwargs) -> str:
            telemetry.record(
                CallRecord(
                    task="patch_suggest",
                    tier="cheap",
                    input_tokens=10,
                    output_tokens=2,
                )
            )
            return "[]"

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (FakeGateway(), telemetry),
    )
    result = engineering_actions.repair_plan_action(
        root,
        max_iterations=2,
        llm_candidates=True,
        max_llm_calls=1,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.01,
    )
    assert result["candidate_source"] == "deterministic_plus_model"
    assert result["used_llm"] is True
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.01
    assert result["writes_canon"] is False


def test_style_sweep_action_wires_bounded_judge_and_reports_mode(tmp_path, monkeypatch) -> None:
    import owcopilot.app.actions.engineering as engineering_actions

    root = tmp_path / "style-action-content"
    ContentStore(root).save(_world())
    telemetry = TelemetryCollector()
    observed: dict[str, object] = {}

    class FakeGateway:
        pass

    class FakeRubricSweepService:
        def __init__(self, *, bundle, gateway, embedder) -> None:
            observed["gateway"] = gateway

        def sweep(self, rubric, **kwargs):
            observed.update(kwargs)
            telemetry.record(
                CallRecord(
                    task="theme_sweep",
                    tier="cheap",
                    input_tokens=20,
                    output_tokens=4,
                )
            )
            return SimpleNamespace(
                rubric_id=rubric.id,
                rubric_label=rubric.label,
                rubric_version=rubric.version,
                rubric_fingerprint="fingerprint",
                abstract_tag=rubric.abstract_tag,
                scanned_total=1,
                scanned_by_kind={"entity": 1},
                scanned_fields=2,
                semantic_used=False,
                semantic_threshold=0.0,
                judged_count=1,
                judge_skipped=0,
                dismissed_count=0,
                l1_total=1,
                l2_total=0,
                l1_l2_overlap=0,
                hits=[],
                review_suggested=[],
                elements=[],
            )

    monkeypatch.setattr(
        engineering_actions,
        "_gateway",
        lambda **_kwargs: (FakeGateway(), telemetry),
    )
    monkeypatch.setattr(engineering_actions, "RubricSweepService", FakeRubricSweepService)
    monkeypatch.setattr(engineering_actions, "render_rubric_markdown", lambda _report: "report")

    result = engineering_actions.style_sweep_action(
        root,
        rubric_id="wafu",
        use_llm=True,
        max_judge=7,
        llm_mode="real",
        llm_model="test-model",
        max_cost_usd=0.02,
    )
    assert observed["use_llm"] is True
    assert observed["max_judge"] == 7
    assert result["judge_mode"] == "real_model"
    assert result["llm_used"] is True
    assert result["telemetry"]["calls"] == 1
    assert result["cost_budget"]["budget_usd"] == 0.02
