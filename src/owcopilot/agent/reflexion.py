# Reflexion 的 Self-Reflection 组件（M_sr，Shinn 2023）：把一次失败试验的 evaluator 反馈蒸馏成
# 一条口头反思，供 multi_agent/session.py 的单次 evaluator 驱动重试前馈给 Actor。要点：
# ① 反思蒸馏空输出即报 ReflexionError、绝不静默降级；② 离线 $0 走确定性模板替身
# （OWCOPILOT_ALLOW_OFFLINE_LLM 门禁），不泄漏进产品路径。
"""Reflexion Self-Reflection component (M_sr) — Shinn et al., 2023, NeurIPS (arXiv:2303.11366).

Reflexion's contract is the triangle Actor (M_a) → Evaluator (M_e) → Self-Reflection (M_sr). This
module owns ONLY the Self-Reflection piece: :func:`distill_reflection` turns a failed trial's
evaluator feedback into ONE concise verbal reflection (what went wrong + how the next attempt should
do better). Reinforcement is via language, not weights.

The bounded, evaluator-driven trial control flow that CONSUMES the reflection lives where the real
orchestration is — the single-shot retry in
:meth:`owcopilot.multi_agent.session.MultiAgentSession._reflexion_retry`: the Evaluator is the
:class:`~owcopilot.multi_agent.verifier.VerifierAgent` verdict already computed in the main loop,
and the Actor is the same ``WorkerAgent`` re-run with the reflection injected as episodic memory
via ``ReActAgent.run(reflections=...)``. Keeping the distillation reusable while letting the
orchestrator own the loop matches the paper's reference implementation (noahshinn/reflexion), where
the trial driver is task-shaped rather than a standalone generic API.

The offline deterministic template ``_offline_reflection`` is the project-sanctioned $0 stand-in for
the Self-Reflection LLM (gated by ``OWCOPILOT_ALLOW_OFFLINE_LLM``, exactly like
``OfflineReactChatModel`` substitutes the actor LLM) — it never leaks into a real deployment, which
always takes the LLM path. A blank model reply raises :class:`ReflexionError` rather than degrading
silently into plain retry.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from ..errors import OWCopilotError
from ..llm.gateway import offline_llm_allowed
from ..llm.telemetry_callback import TelemetryCallbackHandler
from .react import _AGENT_TIER, ReActAgent, _model_id_of


class ReflexionError(OWCopilotError, RuntimeError):
    """Raised when self-reflection cannot produce a real reflection (blank model output).

    Guarantees "no silent downgrade": rather than feeding an empty reflection forward (which would
    quietly weaken the loop into plain retry), the episode fails loudly so the caller knows the
    Self-Reflection model misbehaved.
    """


_REFLECTION_SYSTEM_PROMPT = (
    "You are the self-reflection component of a Reflexion agent (Shinn et al., 2023). A prior "
    "attempt at the goal failed its evaluation. Given the goal and the evaluator's feedback, write "
    "ONE concise sentence: what went wrong and how the next attempt should do better. Output only "
    "that reflection — no preamble, no restating the goal."
)


def distill_reflection(*, agent: ReActAgent, goal: str, trial_index: int, feedback: str) -> str:
    """Convert a failed trial's ``feedback`` into one short verbal reflection (the paper's
    Self-Reflection model). One LLM call per failed trial, through the agent's own chat model
    (billed to the agent's collector under its task label, when one is wired).

    Offline ($0) path: when ``OWCOPILOT_ALLOW_OFFLINE_LLM`` is set (tests / CI / eval), use the
    deterministic template — the sanctioned stand-in for the Self-Reflection LLM. A real deployment
    never sets that opt-in, so it always takes the LLM path. Either way the result is guaranteed
    non-empty: a blank model reply raises :class:`ReflexionError` rather than degrading silently.
    """
    if offline_llm_allowed():
        return _offline_reflection(trial_index=trial_index, feedback=feedback)

    config: RunnableConfig | None = None
    if agent.telemetry is not None:
        # Same per-call accounting as the agent loop itself: distillation happens OUTSIDE an
        # agent.invoke context, so the callback handler must be attached explicitly here.
        config = {
            "callbacks": [
                TelemetryCallbackHandler(
                    agent.telemetry,
                    tier=_AGENT_TIER,
                    model=_model_id_of(agent.model),
                    default_task=agent.task,
                )
            ]
        }
    reply = agent.model.invoke(
        [
            SystemMessage(content=_REFLECTION_SYSTEM_PROMPT),
            HumanMessage(
                content=_reflection_prompt(goal=goal, trial_index=trial_index, feedback=feedback)
            ),
        ],
        config=config,
    )
    reflection = str(reply.text or "").strip()
    if not reflection:
        raise ReflexionError(
            f"self-reflection produced an empty reflection for trial {trial_index} "
            "(the model returned blank text); refusing to feed an empty reflection forward"
        )
    return reflection


def _reflection_prompt(*, goal: str, trial_index: int, feedback: str) -> str:
    return (
        f"Goal: {goal}\n"
        f"Attempt {trial_index + 1} failed its evaluation.\n"
        f"Evaluator feedback: {feedback}\n\n"
        "Write the reflection now."
    )


def _offline_reflection(*, trial_index: int, feedback: str) -> str:
    """Deterministic $0 stand-in for the Self-Reflection LLM (gated by OWCOPILOT_ALLOW_OFFLINE_LLM).

    Mirrors the house style of ``assist.refine.summarize_reflection``: a short verbal note stating
    which attempt failed, the evaluator's feedback verbatim, and a forward-looking correction.
    """
    note = feedback.strip() or "（无反馈）"
    return f"第{trial_index + 1}次尝试未通过：{note}。下次应据此调整工具选择与结论。"
