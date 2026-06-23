"""Deterministic, $0 stand-in for the agent's reasoning model — the offline test/CI/eval double.

Like every other offline provider in the project, this is a fixture, never a shipped product mode
(the runtime builders gate it behind ``OWCOPILOT_ALLOW_OFFLINE_LLM``). It emits a fixed, sensible
ReAct trajectory in the canonical text format — audit → grounding lookup → quality harness → report
— and, critically, it *reads the transcript it is given*: its Final Answer quotes the open-error
count scraped from the appended observations, so a passing test proves the loop really feeds tool
results back into the model (not just that it can replay a script).
"""

from __future__ import annotations

import re

_OPEN_ERRORS_RE = re.compile(r'"open_errors":\s*(\d+)')


class OfflineReactProvider:
    """Implements the structural ``llm.gateway.LLMProvider`` protocol."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        # The number of observations already appended tells us how far along the loop is.
        observations = user.count("Observation:")
        text = self._step(observations, transcript=user)
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok

    def _step(self, observations: int, *, transcript: str) -> str:
        if observations == 0:
            return (
                "Thought: I should first run the consistency audit to see what is broken.\n"
                "Action: audit_project\n"
                "Action Input: {}"
            )
        if observations == 1:
            return (
                "Thought: There may be issues; let me pull the canon around the main quest giver "
                "to ground any fix.\n"
                "Action: build_context_pack\n"
                'Action Input: {"query": "main quest giver"}'
            )
        if observations == 2:
            return (
                "Thought: Now get the consolidated quality state and shadow-validated fix "
                "proposals.\n"
                "Action: quality_harness\n"
                "Action Input: {}"
            )
        open_errors = self._scrape_open_errors(transcript)
        return (
            "Thought: I have audited the world, grounded the context, and gathered fix proposals. "
            "I can report now.\n"
            f"Final Answer: 一致性审计发现 {open_errors} 个待修复错误。已生成经影子校验的修复"
            "提案；请在审阅队列中确认后再写入正典。"
        )

    @staticmethod
    def _scrape_open_errors(transcript: str) -> str:
        matches = _OPEN_ERRORS_RE.findall(transcript)
        return matches[-1] if matches else "未知数量的"
