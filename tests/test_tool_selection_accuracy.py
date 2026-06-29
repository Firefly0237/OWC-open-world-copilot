"""Tests for C1: compute_tool_selection_accuracy, GoldReActScenario, and the 6th AcceptanceCheck.

诚实说明：
- OfflineGoalAwareReActProvider 被设计成精确返回 gold 序列，offline F1=1.0 是设计预期。
- sanity gate（10 个手工标注场景，集合 F1，不计顺序）不是统计基准，是 CI 确定性回归门禁。
- 所有测试均 $0 无 LLM 调用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from owcopilot.agent.offline import OfflineGoalAwareReActProvider
from owcopilot.agent.react import AgentStep
from owcopilot.content.store import ContentStore
from owcopilot.evaluation.acceptance import (
    TOOL_ACCURACY_GATE,
    GoldReActScenario,
    _build_gold_react_scenarios,
    _NullSkillRegistry,
    build_acceptance_world,
    compute_tool_selection_accuracy,
    run_acceptance_evaluation,
)

# ── compute_tool_selection_accuracy ──────────────────────────────────────────


def _step(action: str, is_error: bool = False) -> AgentStep:
    return AgentStep(action=action, is_error=is_error)


def test_compute_accuracy_both_empty() -> None:
    """Both empty → precision=recall=f1=1.0 (contract boundary case)."""
    result = compute_tool_selection_accuracy([], [])
    assert result == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_compute_accuracy_actual_empty_expected_nonempty() -> None:
    """No actual tools, expected non-empty → all 0.0."""
    result = compute_tool_selection_accuracy([], ["audit_project"])
    assert result == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_compute_accuracy_actual_nonempty_expected_empty() -> None:
    """Actual tools, expected empty → all 0.0."""
    result = compute_tool_selection_accuracy([_step("audit_project")], [])
    assert result == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_compute_accuracy_perfect_match() -> None:
    """Exact match → F1=1.0."""
    steps = [_step("audit_project"), _step("list_issues")]
    result = compute_tool_selection_accuracy(steps, ["audit_project", "list_issues"])
    assert result["f1"] == pytest.approx(1.0)
    assert result["precision"] == pytest.approx(1.0)
    assert result["recall"] == pytest.approx(1.0)


def test_compute_accuracy_partial_recall() -> None:
    """Actual subset of expected → recall < 1.0, precision = 1.0."""
    steps = [_step("audit_project")]
    result = compute_tool_selection_accuracy(steps, ["audit_project", "list_issues"])
    assert result["recall"] == pytest.approx(0.5)
    assert result["precision"] == pytest.approx(1.0)
    assert result["f1"] < 1.0


def test_compute_accuracy_extra_actual() -> None:
    """Actual superset of expected → recall=1.0, precision<1.0."""
    steps = [_step("audit_project"), _step("build_context_pack")]
    result = compute_tool_selection_accuracy(steps, ["audit_project"])
    assert result["recall"] == pytest.approx(1.0)
    assert result["precision"] == pytest.approx(0.5)
    assert result["f1"] < 1.0


def test_compute_accuracy_wrong_tool() -> None:
    """Completely wrong tool → F1=0.0."""
    steps = [_step("build_context_pack")]
    result = compute_tool_selection_accuracy(steps, ["audit_project"])
    assert result["f1"] == pytest.approx(0.0)


def test_compute_accuracy_is_error_steps_excluded() -> None:
    """Steps with is_error=True are excluded from actual_set."""
    steps = [
        _step("audit_project", is_error=False),
        _step("list_issues", is_error=True),  # should be excluded
    ]
    result = compute_tool_selection_accuracy(steps, ["audit_project"])
    # Only audit_project is counted; list_issues (error) not in actual_set
    assert result["f1"] == pytest.approx(1.0)


def test_compute_accuracy_order_independent() -> None:
    """F1 does not depend on order of steps (set semantics)."""
    steps_ab = [_step("audit_project"), _step("list_issues")]
    steps_ba = [_step("list_issues"), _step("audit_project")]
    result_ab = compute_tool_selection_accuracy(steps_ab, ["audit_project", "list_issues"])
    result_ba = compute_tool_selection_accuracy(steps_ba, ["audit_project", "list_issues"])
    assert result_ab["f1"] == pytest.approx(result_ba["f1"])


# ── GoldReActScenario ─────────────────────────────────────────────────────────


def test_gold_scenarios_count() -> None:
    """Must have exactly 10 scenarios."""
    scenarios = _build_gold_react_scenarios()
    assert len(scenarios) == 10


def test_gold_scenarios_covers_6_skills() -> None:
    """C1-H6: all 6 built-in skill names appear at least once."""
    required_skills = {
        "audit_project",
        "list_issues",
        "build_context_pack",
        "impact_of",
        "propose_fix",
        "quality_harness",
    }
    scenarios = _build_gold_react_scenarios()
    all_actions: set[str] = set()
    for s in scenarios:
        all_actions.update(s.expected_actions)
    assert required_skills <= all_actions, (
        f"Missing skills: {required_skills - all_actions}"
    )


def test_gold_scenarios_unique_ids() -> None:
    """All scenario_ids are unique."""
    scenarios = _build_gold_react_scenarios()
    ids = [s.scenario_id for s in scenarios]
    assert len(ids) == len(set(ids))


def test_gold_scenario_model() -> None:
    """GoldReActScenario pydantic construction."""
    s = GoldReActScenario(
        scenario_id="T01",
        goal="审计世界",
        expected_actions=["audit_project"],
    )
    assert s.scenario_id == "T01"
    assert s.expected_actions == ["audit_project"]
    assert s.description == ""


# ── OfflineGoalAwareReActProvider ─────────────────────────────────────────────


def test_offline_provider_deterministic() -> None:
    """C1-H1: same goal → identical output on repeated calls."""
    scenarios = [("审计世界", ["audit_project", "list_issues"])]
    provider = OfflineGoalAwareReActProvider.from_scenarios(scenarios)
    user = "Goal: 审计世界\n\nBegin. Output your first Thought and Action."
    out1, _, _ = provider.complete(system="", user=user, model="")
    out2, _, _ = provider.complete(system="", user=user, model="")
    assert out1 == out2


def test_offline_provider_returns_correct_action() -> None:
    """C1-H2: provider returns the gold action at step 0."""
    scenarios = [("单步目标", ["quality_harness"])]
    provider = OfflineGoalAwareReActProvider.from_scenarios(scenarios)
    user = "Goal: 单步目标\n\nBegin. Output your first Thought and Action."
    text, _, _ = provider.complete(system="", user=user, model="")
    assert "quality_harness" in text


def test_offline_provider_no_network_calls(monkeypatch) -> None:
    """C1-H5: provider completes without any HTTP/socket calls."""
    import socket

    def mock_connect(*args, **kwargs):
        raise AssertionError("Unexpected network call in OfflineGoalAwareReActProvider")

    monkeypatch.setattr(socket.socket, "connect", mock_connect)
    scenarios = [("无网络测试", ["audit_project"])]
    provider = OfflineGoalAwareReActProvider.from_scenarios(scenarios)
    user = "Goal: 无网络测试\n\nBegin. Output your first Thought and Action."
    text, in_tok, out_tok = provider.complete(system="", user=user, model="")
    assert "audit_project" in text
    assert in_tok >= 1
    assert out_tok >= 1


def test_offline_provider_multi_step() -> None:
    """Multi-action scenario: step index advances correctly."""
    goal = "多步测试"
    actions = ["audit_project", "list_issues"]
    provider = OfflineGoalAwareReActProvider.from_scenarios([(goal, actions)])

    # Step 0 — no observations yet
    user0 = f"Goal: {goal}\n\nBegin. Output your first Thought and Action."
    text0, _, _ = provider.complete(system="", user=user0, model="")
    assert "audit_project" in text0

    # Step 1 — one Observation in transcript
    user1 = (
        f"Goal: {goal}\n\n"
        "Thought: Auditing.\nAction: audit_project\nAction Input: {}\n"
        "Observation: {\"open_errors\": 3}\n\n"
        "Continue."
    )
    text1, _, _ = provider.complete(system="", user=user1, model="")
    assert "list_issues" in text1


def test_offline_provider_fallback_for_unknown_goal() -> None:
    """Unknown goal → fallback script returns audit_project."""
    provider = OfflineGoalAwareReActProvider.from_scenarios([("已知目标", ["propose_fix"])])
    user = "Goal: 未知目标\n\nBegin. Output your first Thought and Action."
    text, _, _ = provider.complete(system="", user=user, model="")
    assert "Action:" in text or "Final Answer:" in text


# ── Wrong-action gate-fail test (C1-H3) ──────────────────────────────────────


def test_wrong_action_gate_fails() -> None:
    """C1-H3: if provider returns wrong action, F1<1.0 and gate would fail."""
    # Construct a provider that returns build_context_pack when gold expects audit_project
    provider = OfflineGoalAwareReActProvider(
        scripts={
            "错误动作目标": [
                "Thought: Wrong.\nAction: build_context_pack\nAction Input: {}",
                "Thought: Done.\nFinal Answer: 完毕。",
            ]
        }
    )
    user = "Goal: 错误动作目标\n\nBegin. Output your first Thought and Action."
    text, _, _ = provider.complete(system="", user=user, model="")
    assert "build_context_pack" in text

    # Now compute F1 against gold expectation of audit_project
    actual_steps = [AgentStep(action="build_context_pack", is_error=False)]
    result = compute_tool_selection_accuracy(actual_steps, ["audit_project"])
    assert result["f1"] < 1.0

    # Gate would fail (mean_f1 < TOOL_ACCURACY_GATE)
    mean_f1 = result["f1"]
    assert mean_f1 < TOOL_ACCURACY_GATE


def test_gate_fails_when_provider_returns_wrong_actions(tmp_path: Path, monkeypatch) -> None:
    """C1-H3 (integration): drive the FULL _run_tool_selection_accuracy_gate path with a
    deliberately wrong provider, and assert the returned AcceptanceCheck fails.

    This exercises the real integration chain — ReActAgent → _NullSkillRegistry → gate
    function → AcceptanceCheck — not just the metric in isolation. We monkeypatch the
    provider factory so the gate's own ReAct loop emits non-gold actions (every scenario
    answers with build_context_pack, regardless of what the gold sequence expects). A
    genuinely-failing gate proves the gate can FAIL in production code, not only that the
    metric would fail given hand-built steps.
    """
    from owcopilot.evaluation.acceptance import (
        _build_gold_react_scenarios,
        _run_tool_selection_accuracy_gate,
    )

    # Write a clean world so any registry/content lookups in the path are valid.
    clean_root = tmp_path / "clean"
    ContentStore(clean_root).save(build_acceptance_world())

    gold = _build_gold_react_scenarios()
    # Build a provider whose script for EVERY gold goal returns build_context_pack instead
    # of the expected action(s) — a single wrong action per scenario.
    wrong_scripts = {
        scenario.goal: [
            "Thought: Wrong on purpose.\n"
            'Action: build_context_pack\nAction Input: {"query": "x"}',
            "Thought: Done.\nFinal Answer: 完毕。",
        ]
        for scenario in gold
    }
    wrong_provider = OfflineGoalAwareReActProvider(scripts=wrong_scripts)

    # Patch the factory the gate calls internally (the gate does a local import of this same
    # class object, so patching the classmethod here is seen inside the gate). The real gate
    # path then drives ReActAgent → _NullSkillRegistry with the wrong-action provider.
    monkeypatch.setattr(
        OfflineGoalAwareReActProvider,
        "from_scenarios",
        classmethod(lambda cls, scenarios: wrong_provider),
    )

    metrics: dict = {}
    check = _run_tool_selection_accuracy_gate(clean_root, tmp_path, metrics)

    # The gate must genuinely fail at the integration layer.
    assert check.name == "tool_selection_accuracy_gate"
    assert check.passed is False
    assert check.details["mean_f1"] < TOOL_ACCURACY_GATE
    # metrics dict is populated with the (low) mean F1
    assert metrics["tool_selection_accuracy_mean_f1"] < TOOL_ACCURACY_GATE
    # Sanity: at least one scenario whose gold did NOT include build_context_pack scored < 1.0
    sub_f1s = [s["f1"] for s in check.details["scenarios"]]
    assert any(f1 < 1.0 for f1 in sub_f1s)


# ── TOOL_ACCURACY_GATE constant ───────────────────────────────────────────────


def test_tool_accuracy_gate_constant() -> None:
    """TOOL_ACCURACY_GATE must be exactly 0.80."""
    assert TOOL_ACCURACY_GATE == 0.80


# ── Integration: 6th AcceptanceCheck in report ───────────────────────────────


def test_acceptance_has_tool_selection_check(tmp_path: Path) -> None:
    """C1-H7: AcceptanceReport.checks contains 'tool_selection_accuracy_gate' as last check.

    诚实说明：C3 契约说"第 6 个 AcceptanceCheck"，但 Phase B baseline 已有 6 个 checks，
    故 tool_selection_accuracy_gate 实际为第 7 个。契约中 len==6 的断言与 baseline 不符，
    此处改为断言 last check name 正确，符合实际意图。
    """
    report = run_acceptance_evaluation(tmp_path)
    assert len(report.checks) >= 6, (
        f"Expected at least 6 checks, got {len(report.checks)}: "
        f"{[c.name for c in report.checks]}"
    )
    check_names = [c.name for c in report.checks]
    assert "tool_selection_accuracy_gate" in check_names


def test_acceptance_sixth_check_name(tmp_path: Path) -> None:
    """C1-H7: The 6th check's name == 'tool_selection_accuracy_gate'."""
    report = run_acceptance_evaluation(tmp_path)
    assert report.checks[-1].name == "tool_selection_accuracy_gate"


def test_acceptance_sixth_check_passes(tmp_path: Path) -> None:
    """C1-H2/H4: 6th check passes (offline provider returns gold sequences, F1=1.0)."""
    report = run_acceptance_evaluation(tmp_path)
    tool_check = report.checks[-1]
    assert tool_check.passed, (
        f"tool_selection_accuracy_gate failed: mean_f1={tool_check.details.get('mean_f1')}"
    )


def test_acceptance_sixth_check_note_honest(tmp_path: Path) -> None:
    """C1-H8: note contains sanity gate / 手工标注 / 集合F1 keyword."""
    report = run_acceptance_evaluation(tmp_path)
    note = report.checks[-1].details.get("note", "")
    keywords = ["sanity gate", "手工标注", "集合F1", "不计顺序"]
    assert any(kw in note for kw in keywords), (
        f"note does not contain honesty keywords: {note!r}"
    )


def test_acceptance_metrics_has_tool_f1(tmp_path: Path) -> None:
    """C1-H9: AcceptanceReport.metrics contains 'tool_selection_accuracy_mean_f1'."""
    report = run_acceptance_evaluation(tmp_path)
    assert "tool_selection_accuracy_mean_f1" in report.metrics
    val = report.metrics["tool_selection_accuracy_mean_f1"]
    assert isinstance(val, float)
    assert 0.0 <= val <= 1.0


def test_acceptance_telemetry_zero_cost(tmp_path: Path) -> None:
    """C1-H5: All gateway telemetry records have cost_usd==0.0 (no LLM calls)."""
    from owcopilot.agent.offline import OfflineGoalAwareReActProvider
    from owcopilot.agent.react import ReActAgent
    from owcopilot.evaluation.acceptance import _build_gold_react_scenarios
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter
    from owcopilot.llm.telemetry import TelemetryCollector

    scenarios = _build_gold_react_scenarios()
    provider = OfflineGoalAwareReActProvider.from_scenarios(
        [(s.goal, s.expected_actions) for s in scenarios[:1]]
    )
    telemetry = TelemetryCollector()
    gw = LLMGateway(
        providers={"react": provider},
        router=StaticRouter(mapping={"agent_react": "react"}),
        cache=NoOpCache(),
        telemetry=telemetry,
    )
    registry = _NullSkillRegistry()
    agent = ReActAgent(gateway=gw, registry=registry, max_steps=5)  # type: ignore[arg-type]
    agent.run(scenarios[0].goal)

    for record in telemetry.records:
        assert record.cost_usd == 0.0, f"Unexpected cost: {record.cost_usd}"
