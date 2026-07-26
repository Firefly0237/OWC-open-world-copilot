# agent 推理模型的离线替身（$0 确定性，供测试/CI/eval，需 OWCOPILOT_ALLOW_OFFLINE_LLM）。
# 按固定合理的 ReAct 轨迹走（审计→接地→质量 harness→报告），让整条 agent 链路无需真模型即可测。
# LangChain chat model 形态(tool_calls 剧本)，喂真实的 create_agent 循环；
# 文本 ReAct 版 Offline*Provider 已随文本路径退役(T2 S8)。
"""Deterministic, $0 stand-ins for the agent's reasoning model — the offline test/CI/eval doubles.

Like every other offline provider in the project, these are fixtures, never a shipped product mode
(the runtime builders gate them behind ``OWCOPILOT_ALLOW_OFFLINE_LLM``). Each is a LangChain
``BaseChatModel`` fake emitting a fixed, sensible ReAct trajectory as native ``tool_calls`` —
audit → grounding lookup → quality harness → report — driving the real
``langchain.agents.create_agent`` loop. Critically, the transcript-aware one *reads the
conversation it is given*: the final answer quotes the open-error count scraped from the appended
tool results, so a passing test proves the loop really feeds tool results back into the model
(not just that it can replay a script). The text-format ``Offline*Provider`` doubles retired
together with the text ReAct path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, ClassVar

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_OPEN_ERRORS_RE = re.compile(r'"open_errors":\s*(\d+)')


def _tool_call_message(thought: str, name: str, args: dict[str, Any], *, seq: int) -> AIMessage:
    """One scripted assistant turn requesting a single tool call (deterministic ``call_{seq}``)."""
    return AIMessage(
        content=thought,
        tool_calls=[{"name": name, "args": args, "id": f"call_{seq}", "type": "tool_call"}],
    )


def _observation_count(messages: Sequence[BaseMessage]) -> int:
    """How many tool results the conversation already carries — the loop-progress cursor."""
    return sum(1 for m in messages if isinstance(m, ToolMessage))


class OfflineReactChatModel(BaseChatModel):
    """Transcript-aware LangChain fake: the canonical trajectory as native ``tool_calls``.

    The canonical trajectory double: audit → grounding lookup →
    quality harness → report, where the final answer quotes the real ``open_errors`` count
    scraped from the ``ToolMessage`` results it was handed — proof the agent loop feeds tool
    output back into the model. $0 deterministic; a test fixture, never a product path.
    """

    @property
    def _llm_type(self) -> str:
        return "offline-react-chat-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        """The script decides its own tool calls; binding is a no-op (create_agent always binds)."""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._turn(messages))])

    def _turn(self, messages: Sequence[BaseMessage]) -> AIMessage:
        observations = _observation_count(messages)
        if observations == 0:
            return _tool_call_message(
                "I should first run the consistency audit to see what is broken.",
                "audit_project",
                {},
                seq=0,
            )
        if observations == 1:
            return _tool_call_message(
                "There may be issues; let me pull the canon around the main quest giver "
                "to ground any fix.",
                "build_context_pack",
                {"query": "main quest giver"},
                seq=1,
            )
        if observations == 2:
            return _tool_call_message(
                "Now get the consolidated quality state and shadow-validated fix proposals.",
                "quality_harness",
                {},
                seq=2,
            )
        open_errors = self._scrape_open_errors(messages)
        return AIMessage(
            content=(
                f"一致性审计发现 {open_errors} 个待修复错误。已生成经影子校验的修复"
                "提案；请在审阅队列中确认后再写入正典。"
            )
        )

    @staticmethod
    def _scrape_open_errors(messages: Sequence[BaseMessage]) -> str:
        matches: list[str] = []
        for m in messages:
            if isinstance(m, ToolMessage):
                matches.extend(_OPEN_ERRORS_RE.findall(str(m.content)))
        return matches[-1] if matches else "未知数量的"


#: One scripted turn of the goal-aware fake: ``(tool_name, args)`` or a final answer string.
GoalScriptTurn = tuple[str, dict[str, Any]] | str


class OfflineGoalAwareChatModel(BaseChatModel):
    """Goal-text-aware deterministic chat-model fake for eval use only.

    ``scripts`` maps goal
    text to a pre-authored turn list ``[(tool, args), ..., final_answer_str]``. On each call it
    reads the goal from the first human message (a leading ``Goal:`` prefix is stripped when
    present), counts the tool results already in the conversation, and replays the turn at that
    index — emitted as native ``tool_calls``. $0 deterministic; used only by the
    tool_selection_accuracy eval gate, not a product capability.
    """

    scripts: dict[str, list[GoalScriptTurn]]

    @property
    def _llm_type(self) -> str:
        return "offline-goal-aware-chat-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        """The script decides its own tool calls; binding is a no-op (create_agent always binds)."""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        observations = _observation_count(messages)
        script = self.scripts.get(self._extract_goal(messages), self._fallback_script())
        turn = script[min(observations, len(script) - 1)]
        if isinstance(turn, str):
            message = AIMessage(content=turn)
        else:
            name, args = turn
            message = _tool_call_message(
                f"Step {observations + 1} — will call {name}.", name, args, seq=observations
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _extract_goal(messages: Sequence[BaseMessage]) -> str:
        """The goal text: the first human message, minus a ``Goal:`` header when one is used."""
        for m in messages:
            if isinstance(m, HumanMessage):
                text = str(m.content)
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Goal:"):
                        return stripped[5:].strip()
                return text.strip()
        return ""

    @staticmethod
    def _fallback_script() -> list[GoalScriptTurn]:
        return [("audit_project", {}), "审计完毕。"]

    # Minimal valid args for each built-in skill: skills with required params get a sensible
    # placeholder value so the SkillRegistry does not raise SkillError (is_error=False → F1).
    # ClassVar keeps it out of pydantic's field/private-attr machinery (plain class constant).
    _SKILL_ACTION_ARGS: ClassVar[dict[str, dict[str, Any]]] = {
        "audit_project": {},
        "list_issues": {},
        "quality_harness": {},
        "build_context_pack": {"query": "main entities"},
        "impact_of": {
            "changes": [{"change_type": "entity_delete", "target_ref": "entity:npc_r1_a"}]
        },
        "propose_fix": {"issue_id": "__placeholder__"},
    }

    @classmethod
    def from_scenarios(cls, scenarios: list[tuple[str, list[str]]]) -> OfflineGoalAwareChatModel:
        """Build from ``(goal_text, expected_actions)`` pairs.

        Generates a script per scenario that exactly matches the expected action sequence, then
        appends a final answer — so F1=1.0 for every scenario in the gold set *by construction*
        (the design intent: this pins the harness, it is not a measured performance claim).

        真实性标签：fixture，非 real-model。F1=1.0 是这个替身按定义精确回放 gold 动作序列的必然结
        果，不是任何真实 LLM 的工具选择表现，不得作为对外材料声称的工具选择准确率数字。
        """
        scripts: dict[str, list[GoalScriptTurn]] = {}
        for goal_text, expected_actions in scenarios:
            turns: list[GoalScriptTurn] = [
                (action, cls._SKILL_ACTION_ARGS.get(action, {})) for action in expected_actions
            ]
            action_seq = "→".join(expected_actions) if expected_actions else "（无动作）"
            turns.append(f"已依序执行 {action_seq}，完成目标。")
            scripts[goal_text] = turns
        return cls(scripts=scripts)
