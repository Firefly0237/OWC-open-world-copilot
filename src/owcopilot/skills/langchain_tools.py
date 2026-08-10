# 技能注册表 → LangChain StructuredTool 桥:把每个技能包成 create_agent 可执行的工具。
# 关键:① 参数校验/强转/WRITES_CANON 禁令仍由 registry.run 一家承担(生产路径复用、不重写)——
#   args_schema 用 JSON schema dict(LangChain 对 dict schema 直通不校验,正合此意);
# ② content 用与 agent 循环一致的排序 JSON 自产(字节级 parity),artifact 保住结构化结果
#   (content_and_artifact),供 AgentStep.result 组装。
# ③ 统一响应信封(E-1)的“成功出口”就在这里:payload 即信封的 data,status=success 由 ToolMessage
#   承载,content 是规范 dump——已符合信封契约,故不再包一层(重包会破坏 dumps_observation 字节级
#   parity 不变量);失败出口的 error_code 信封在 middleware.py 经 skills/errors.py 统一。
"""Skill registry → LangChain ``StructuredTool`` bridge.

``create_agent`` treats plain dict tool schemas as provider built-ins (not executable), so each
skill is wrapped as a :class:`~langchain_core.tools.StructuredTool` instead. The wrapping is a
thin pass-through by design:

* ``args_schema`` is the skill's OpenAI JSON schema **dict** — LangChain passes dict-schema input
  through unvalidated, which is exactly right here: argument validation/coercion and the
  WRITES_CANON auto-invocation ban stay with :meth:`SkillRegistry.run` (one surface, the
  production path, never re-implemented).
* ``response_format="content_and_artifact"``: the tool returns ``(content, artifact)`` where
  ``content`` is our own canonical sorted-JSON dump (byte parity with the agent loop's
  observation serialization — ToolNode's default dump does not sort keys) and ``artifact`` is the
  structured result dict, so downstream consumers (``AgentStep.result``) never have to parse the
  possibly-truncated observation string.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool

from . import Skill, SkillRegistry


def dumps_observation(value: Any) -> str:
    """Canonical observation serialization — sorted keys, raw CJK.

    This is the one definition of "what a tool result looks like as an observation string";
    the agent loop's byte-parity contract (untruncated observation == this dump) hangs off it.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _bridge(registry: SkillRegistry, name: str) -> Any:
    """Build the tool function for one skill: every call dispatches through ``registry.run``."""

    def run_skill(**args: Any) -> tuple[str, dict[str, Any] | None]:
        result = registry.run(name, args)
        artifact = result if isinstance(result, dict) else None
        return dumps_observation(result), artifact

    return run_skill


def as_structured_tools(
    registry: SkillRegistry, allowed: set[str] | None = None
) -> list[StructuredTool]:
    """Wrap every (allowed) registered skill as an executable LangChain ``StructuredTool``.

    ``allowed`` follows the same filter contract as ``registry.manifest`` /
    ``registry.openai_tools``: ``None`` exposes all skills, a set exposes only those named in it,
    and unknown names are silently ignored. This is the *schema-layer* permission gate (the model
    never sees a filtered-out tool); the *execution-layer* gate stays with ``registry.run``, which
    every call dispatches through regardless.
    """
    skills: list[Skill] = [s for s in registry if allowed is None or s.name in allowed]
    tools: list[StructuredTool] = []
    for skill in skills:
        metadata: dict[str, Any] = {
            "owc_semantic_projection": skill.semantic_projection_name,
            "owc_state_marker_policy": skill.state_marker_policy,
        }
        if skill.polling is not None:
            metadata["owc_polling_policy"] = {
                "status_field": skill.polling.status_field,
                "pending_values": list(skill.polling.pending_values),
                "max_polls": skill.polling.max_polls,
                "deadline_seconds": skill.polling.deadline_seconds,
            }
        tools.append(
            StructuredTool(
                name=skill.name,
                description=skill.description,
                args_schema=skill.openai_tool_schema()["function"]["parameters"],
                func=_bridge(registry, skill.name),
                response_format="content_and_artifact",
                metadata=metadata,
            )
        )
    return tools
