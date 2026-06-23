"""WS-M · B7 — AI-assisted quest logic drafting.

The native logic layer (WS-A) is hand-authored today. B7 lets a model *draft* the variables /
preconditions / effects / branches, then runs the **deterministic logic audit (WS-A) as the hard
gate** in a refine loop: the model proposes, `audit_quest_logic` disproves, the model fixes — and
whatever survives still goes through human review (HITL). The determinism is the whole point: this
is the one place an "AI writes your quest logic" feature can honestly say *the result was checked*.

No `eval`, no new schema — it produces a `QuestLogic` exactly like a human would, so every
downstream consumer (audit, simulate, content-bundle export) works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from ..assist.critic import CritiqueResult
from ..assist.industry import LOGIC_RUBRIC_SOURCES, industry_source_block
from ..assist.refine import RefineStep, run_refine_loop
from ..content.models import Quest, QuestLogic
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from .audit import LogicIssue, audit_quest_logic

LOGIC_DRAFT_TASK = "draft_quest_logic"
# Sentinel the offline double keys off of, so the deterministic stand-in can drive the loop at $0.
LOGIC_DRAFT_MARKER = "[LOGIC_DRAFT]"
LOGIC_REFINE_MARKER = "[LOGIC_REFINE]"

_SYSTEM = (
    "你是任务逻辑设计助手。只输出一个 JSON 对象，描述任务的逻辑层 QuestLogic，不要解释。\n"
    f"{LOGIC_DRAFT_MARKER}\n" + industry_source_block(*LOGIC_RUBRIC_SOURCES) + "\n"
    "JSON 结构：{\n"
    '  "variables": [{"id":"标识符(英文/下划线)","name":"中文名","type":"bool|int|enum",'
    '"default":false/0/"值","enum_values":["可选枚举值"]}],\n'
    '  "precondition": "可选，任务开始的布尔前置表达式",\n'
    '  "stage_logic": [{"stage_id":"必须是给定阶段之一","precondition":"可选布尔表达式",'
    '"effects_on_complete":[{"var":"已声明变量","op":"set|inc|dec","value":true/数字/\\"值\\""}]}],\n'
    '  "branches": [{"id":"分支id","from_stage":"已给阶段","condition":"布尔表达式",'
    '"to_stage":"已给阶段(或留空表示结局)","outcome":"留空或结局文案",'
    '"effects":[{"var":"已声明变量 或 rep:阵营id","op":"set|inc|dec",'
    '"value":true/数字/\\"值\\""}]}]\n'
    "}\n"
    "表达式规则（安全 DSL，禁止函数调用/属性访问）：只能用已声明变量、字面量、"
    "运算符 and/or/not/==/!=/>/>=/</<=，以及 quest:任务id.done 状态引用、"
    "rep:阵营id 阵营声望(整数)引用。\n"
    "硬性要求：branch 的 stage 必须是给定 id；condition 里的变量必须先声明；"
    "至少有一条从首阶段到达完成的路径。\n"
    "把每个选择的后果写进该 branch 的 effects（不要塞进文案）；阵营声望变化用 rep:阵营id 作 var、"
    "op 用 inc/dec、value 为整数（阵营 id 必须是世界中真实存在的阵营）。"
)


@dataclass
class LogicDraftResult:
    logic: QuestLogic
    issues: list[LogicIssue]  # deterministic audit issues still open on the final draft
    trail: list[RefineStep] = field(default_factory=list)
    auto_review_incomplete: bool = False


def _quest_brief(quest: Quest) -> str:
    stages = "、".join(f"{s.id}（{s.summary}）" for s in quest.stages) or "（无阶段）"
    return (
        f"任务标题：{quest.title}\n"
        f"任务目标：{quest.objective}\n"
        f"阶段（stage_id 只能用这些）：{stages}\n"
        f"任务 id：{quest.id}"
    )


def _user_prompt(quest: Quest, intent: str, feedback: list[str]) -> str:
    parts = [_quest_brief(quest)]
    if intent.strip():
        parts.append(f"设计意图：{intent.strip()}")
    if feedback:
        parts.append(LOGIC_REFINE_MARKER)
        parts.append("上一稿的确定性审计问题，请逐条修正后重新输出完整 QuestLogic JSON：")
        parts.extend(f"- {f}" for f in feedback)
    return "\n".join(parts)


def _parse_logic(raw: str) -> QuestLogic | None:
    try:
        data = extract_json_object(raw)
    except ValueError:
        return None
    # tolerate the model omitting list fields
    for key in ("variables", "stage_logic", "branches", "unlocks"):
        data.setdefault(key, [])
    try:
        return QuestLogic.model_validate(data)
    except ValidationError:
        return None


def draft_quest_logic(
    *,
    gateway: LLMGateway,
    quest: Quest,
    intent: str = "",
    max_rounds: int = 2,
) -> LogicDraftResult:
    """Draft a quest's logic with the model, gated by the deterministic audit. Returns the best
    draft plus any audit issues still open (so review sees them) and the refine trail. Raises
    ValueError if the model never returns parseable logic (honest failure, not a fabricated one)."""

    def generate(feedback: list[str]) -> QuestLogic | None:
        raw = gateway.complete(
            task=LOGIC_DRAFT_TASK, system=_SYSTEM, user=_user_prompt(quest, intent, feedback)
        )
        return _parse_logic(raw)

    initial = generate([])
    if initial is None:  # one honest retry before giving up
        initial = generate(["上次输出无法解析为合法 JSON，请只返回一个 QuestLogic JSON 对象。"])
    if initial is None:
        raise ValueError("模型未能生成可解析的任务逻辑，请重试或改为人工编写。")

    parse_failed = False

    def assess(logic: QuestLogic) -> tuple[list[str], CritiqueResult]:
        issues = audit_quest_logic(quest.model_copy(update={"logic": logic}))
        gaps = [f"{i.code}: {i.message}" for i in issues]
        clean = not issues
        crit = CritiqueResult(
            verdict="pass" if clean else "revise",
            score=1.0 if clean else 0.4,
            dimensions=[],
            summary="确定性逻辑审计通过。" if clean else "逻辑审计发现问题，需修正。",
            parse_ok=True,
        )
        return gaps, crit

    def regenerate(logic: QuestLogic, fixes: list[str]) -> QuestLogic:
        nonlocal parse_failed
        nxt = generate(fixes)
        if nxt is None:  # keep the last good draft rather than fabricate; flag for human scrutiny
            parse_failed = True
            return logic
        return nxt

    outcome = run_refine_loop(
        initial=initial, max_rounds=max_rounds, assess=assess, regenerate=regenerate
    )
    final_issues = audit_quest_logic(quest.model_copy(update={"logic": outcome.artifact}))
    return LogicDraftResult(
        logic=outcome.artifact,
        issues=final_issues,
        trail=outcome.trail,
        auto_review_incomplete=outcome.auto_review_incomplete or parse_failed or bool(final_issues),
    )
