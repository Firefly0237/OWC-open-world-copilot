# agent 运行轨迹落库（情节记忆写入侧）：ReAct run 结束时把完整轨迹持久化，供事后 list/show 复盘。
# 依据：CoALA(2309.02427) episodic memory「store experience from earlier decision cycles」+
# MemGPT(2310.08560) recall memory「no message is lost」。本模块只写完整轨迹；语义召回的读侧
# （embed 索引 + recall）在 agent/episodic.py。
"""Persist an agent run's full trajectory as episodic / recall memory (transcript WRITE side).

CoALA (Sumers et al., 2023) defines *episodic memory* as storing "experience from earlier decision
cycles"; MemGPT (Packer et al., 2023) writes every inference cycle to *recall storage* so "no
message is lost". This module owns the full-transcript WRITE + external-inspectability half: at the
end of a run the whole :class:`~owcopilot.agent.react.AgentResult` transcript (goal, stop_reason,
and every step's thought/action/observation + per-step telemetry) is persisted to the project's
always-on runtime SQLite, where it can be listed and replayed by id. The semantic RETRIEVAL half —
embedding a run's (goal + outcome) and recalling similar past runs / reflections back into an
agent's working context — lives in :mod:`owcopilot.agent.episodic` (MemGPT recall storage +
Reflexion buffer), wired at the same persist point in the CLI.

Best-effort by design: a persistence failure logs a WARNING and returns ``None`` rather than
raising, so it can never break the agent run that already completed (observability must not break
the primary flow). This is *not* a silent swallow — the failure is surfaced on the logger and the
``None`` return is propagated to the CLI payload.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from .react import AgentResult

if TYPE_CHECKING:
    from ..storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)

# The ReAct agent that produces these runs names itself "world-consistency" by default
# (see ReActAgent.__init__'s agent_name). Recorded so a run row is attributable to its agent.
_DEFAULT_AGENT_NAME = "world-consistency"


def persist_agent_run(
    store: SQLiteStore,
    result: AgentResult,
    *,
    run_id: str | None = None,
    agent_name: str = _DEFAULT_AGENT_NAME,
) -> str | None:
    """Persist ``result`` as one agent-run row and return its id (``None`` on failure).

    Reads only from ``result`` (never mutates it), so the agent's return value is byte-identical
    before and after this call. Per-run cost/latency are summed from the transcript the loop already
    recorded (nothing new is computed) and the full ``AgentResult`` is stored as ``transcript_json``
    so the run can be replayed by id. A write failure is logged at WARNING and swallowed (returns
    ``None``): the run itself already succeeded and persistence must not break it.
    """
    rid = run_id or uuid.uuid4().hex
    try:
        store.save_agent_run(
            run_id=rid,
            goal=result.goal,
            stop_reason=result.stop_reason,
            step_count=result.step_count,
            cost_usd=sum(step.cost_usd for step in result.steps),
            latency_ms=sum(step.latency_ms for step in result.steps),
            agent_name=agent_name,
            transcript_json=json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("failed to persist agent run %s: %s", rid, exc)
        return None
    return rid
