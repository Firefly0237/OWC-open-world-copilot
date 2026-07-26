# 执行期工具作用域：限定每个 worker 能调的工具面（最小权限）。已提升到 skills 包，此处仅兼容再导出。
"""Backward-compatibility re-export for execution-time tool scoping.

``ScopedSkillRegistry`` / ``scoped_registry`` were promoted to the shared :mod:`owcopilot.skills`
package (``owcopilot.skills.scope``) once the single-agent CLI ``--skills`` path needed the same
execution-layer enforcement — it is a general skill-permission concern, not a multi-agent-only one.
This module keeps the historical import path (``owcopilot.multi_agent.skill_scope``) working for
the workers, the verifier, and existing tests.
"""

from __future__ import annotations

from ..skills.scope import ScopedSkillRegistry, scoped_registry

__all__ = ["ScopedSkillRegistry", "scoped_registry"]
