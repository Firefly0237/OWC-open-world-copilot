"""A ReAct agent (Yao et al., 2022) over the OWCopilot skill surface.

This is the *canonical*, text-based ReAct loop — the original paper's form, which predates
function-calling APIs and fits this project's text-in/text-out gateway exactly. At each turn the
model emits::

    Thought: <reasoning>
    Action: <one skill name>
    Action Input: <a dict of arguments>

the loop executes that skill, appends an ``Observation:`` with the real result, and repeats until
the model emits ``Final Answer:`` or the step budget runs out.

What makes this more than a demo: every Observation is the output of a *deterministic* tool
(the consistency audit and friends), not the model's own guess. So the agent's conclusions are
grounded in ground truth, and its entire action space is read-only / propose-only — the human
review queue remains the only path that writes canon.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..core.skills import SkillError, SkillRegistry
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object

# Cap a single observation fed back into the prompt so a large tool result can't blow the context
# budget. ~1k tokens comfortably fits a typical tool result (an audit of a small/medium world);
# genuinely huge outputs are truncated with an explicit marker (the model sees it), never silently.
_OBSERVATION_CHAR_LIMIT = 4000


class ParsedStep(BaseModel):
    """One parsed model turn: either an action to take, or a final answer."""

    thought: str = ""
    action: str | None = None
    action_input: dict[str, Any] = Field(default_factory=dict)
    final_answer: str | None = None


class AgentStep(BaseModel):
    """One executed action and the observation it produced (surfaced for transparency)."""

    thought: str = ""
    action: str = ""
    action_input: dict[str, Any] = Field(default_factory=dict)
    observation: str = ""
    is_error: bool = False


class AgentResult(BaseModel):
    goal: str
    final_answer: str
    # "finished": model emitted a Final Answer. "max_steps": ran out of step budget.
    stop_reason: str
    steps: list[AgentStep] = Field(default_factory=list)
    step_count: int = 0


_SYSTEM_PROMPT = """You are a world-consistency assistant for an open-world game project. Your job \
is to diagnose the project's content with the available tools and report what is wrong and the \
safe next step. You never write canon: any fix you find goes to a human review queue.

You work in a strict ReAct loop. On each turn output EXACTLY one of these two shapes:

Thought: <your reasoning about what to check next>
Action: <exactly one tool name from the list below>
Action Input: <a dict of arguments for that tool, e.g. {{"query": "main quest giver"}}; use {{}} \
if none>

or, once you have enough to report:

Thought: <your reasoning>
Final Answer: <a concise report for the human: what is wrong, and the safe next step>

Rules:
- Output ONE Thought and ONE Action (or ONE Final Answer). Stop after it.
- Never write an "Observation:" line yourself — the system runs the tool and appends the real \
result. Never invent tool output.
- Use only tool names from this list. Arguments must match the tool's parameters \
(a trailing * marks a required argument).

Available tools:
{manifest}"""


class ReActAgent:
    """Drives the ReAct loop against an :class:`LLMGateway` and a :class:`SkillRegistry`."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        registry: SkillRegistry,
        max_steps: int = 6,
        task: str = "agent_react",
        observation_char_limit: int = _OBSERVATION_CHAR_LIMIT,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.gateway = gateway
        self.registry = registry
        self.max_steps = max_steps
        self.task = task
        self.observation_char_limit = observation_char_limit

    def run(self, goal: str) -> AgentResult:
        system = _SYSTEM_PROMPT.format(manifest=self.registry.manifest())
        transcript: list[str] = []  # the running Thought/Action/Observation scratchpad
        steps: list[AgentStep] = []

        for _ in range(self.max_steps):
            raw = self.gateway.complete(
                task=self.task, system=system, user=_user_prompt(goal, transcript)
            )
            parsed = parse_react_step(raw)

            if parsed.final_answer is not None:
                return AgentResult(
                    goal=goal,
                    final_answer=parsed.final_answer,
                    stop_reason="finished",
                    steps=steps,
                    step_count=len(steps),
                )

            if parsed.action is None:
                # The model produced neither an Action nor a Final Answer. Nudge it once via the
                # transcript and spend a step rather than crashing.
                observation = (
                    "No Action or Final Answer found. Reply with 'Action:' + 'Action Input:' to "
                    "use a tool, or 'Final Answer:' to finish."
                )
                steps.append(
                    AgentStep(thought=parsed.thought, observation=observation, is_error=True)
                )
                transcript.append(_render_turn(parsed.thought, None, {}, observation))
                continue

            try:
                result = self.registry.run(parsed.action, parsed.action_input)
                observation = _truncate(_dumps(result), self.observation_char_limit)
                is_error = False
            except SkillError as exc:
                observation = f"Error: {exc}"
                is_error = True
            except Exception as exc:  # a tool blew up; report it as an observation, don't crash
                observation = f"Error: {type(exc).__name__}: {exc}"
                is_error = True

            steps.append(
                AgentStep(
                    thought=parsed.thought,
                    action=parsed.action,
                    action_input=parsed.action_input,
                    observation=observation,
                    is_error=is_error,
                )
            )
            transcript.append(
                _render_turn(parsed.thought, parsed.action, parsed.action_input, observation)
            )

        return AgentResult(
            goal=goal,
            final_answer=(
                "Reached the step budget before finishing. Latest findings are in the steps above; "
                "re-run with a higher --max-steps to continue."
            ),
            stop_reason="max_steps",
            steps=steps,
            step_count=len(steps),
        )


def parse_react_step(raw: str) -> ParsedStep:
    """Parse one model turn into a :class:`ParsedStep`.

    Robust to the usual model drift: a hallucinated ``Observation:`` (we cut it off — the system
    owns observations), prose/fences around the Action Input dict (handled by the shared
    ``extract_json_object``), and back-ticked or punctuated tool names.
    """
    # The model must not author observations; if it tried, keep only the text before the first one.
    text = raw.split("Observation:", 1)[0].strip()

    thought = _section_after(text, "Thought:", stop_markers=("Action:", "Final Answer:"))

    final = _section_after(text, "Final Answer:", stop_markers=())
    if final:
        return ParsedStep(thought=thought, final_answer=final)

    if "Action:" not in text:
        return ParsedStep(thought=thought)

    after_action = text.split("Action:", 1)[1]
    # The tool name is the first non-empty line after "Action:" (before any Action Input).
    name_region = after_action.split("Action Input:", 1)[0]
    action = _clean_action_name(name_region)
    if not action:
        return ParsedStep(thought=thought)

    action_input: dict[str, Any] = {}
    if "Action Input:" in after_action:
        input_region = after_action.split("Action Input:", 1)[1].strip()
        if input_region and input_region not in {"{}", "none", "None", "null"}:
            try:
                action_input = extract_json_object(input_region)
            except ValueError:
                action_input = {}
    return ParsedStep(thought=thought, action=action, action_input=action_input)


def _section_after(text: str, marker: str, *, stop_markers: tuple[str, ...]) -> str:
    if marker not in text:
        return ""
    region = text.split(marker, 1)[1]
    cut = len(region)
    for stop in stop_markers:
        idx = region.find(stop)
        if idx != -1:
            cut = min(cut, idx)
    return region[:cut].strip()


def _clean_action_name(region: str) -> str:
    for line in region.splitlines():
        candidate = line.strip().strip("`'\"* ").rstrip(".").strip()
        if candidate:
            return candidate
    return ""


def _user_prompt(goal: str, transcript: list[str]) -> str:
    parts = [f"Goal: {goal}", ""]
    if transcript:
        parts.append("\n\n".join(transcript))
        parts.append("")
        parts.append("Continue. Output your next Thought and Action, or a Final Answer.")
    else:
        parts.append("Begin. Output your first Thought and Action.")
    return "\n".join(parts)


def _render_turn(
    thought: str, action: str | None, action_input: dict[str, Any], observation: str
) -> str:
    lines = [f"Thought: {thought}"]
    if action is not None:
        lines.append(f"Action: {action}")
        lines.append(f"Action Input: {_dumps(action_input)}")
    lines.append(f"Observation: {observation}")
    return "\n".join(lines)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"
