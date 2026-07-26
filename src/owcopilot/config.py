# 项目集中默认值 / 关键常量单一来源(D-2 / C-10):把此前在多个模块里各写一份、必须彼此保持
#   一致的"散落常量"收敛到一处,引用方从这里读。只收真正跨文件重复(改一处要同步改多处)的
#   关键常量——阈值 / 预算 / 默认值这类;一次性局部常量仍留在其使用点,不搬进来。
"""Single source of truth for a few constants that were otherwise duplicated across modules.

Only genuinely *scattered* constants live here — a value that had to be repeated (and kept in
sync) across two or more independent definitions. Moving them to one place means a re-tune edits
one line, not several. One-off local constants stay at their use site; this module is deliberately
small and holds no derived or computed values.

Zero intra-package imports so any module can read it without an import cycle. Every value is
byte-identical to the literal it replaced — this is a relocation, not a behaviour change.
"""

from __future__ import annotations

# --------------------------------------------------------------- agent context & memory (C-10)
#: Fraction of the transcript token budget that triggers read-time context compression. Was the
#: default of ``ReActAgent`` / ``ContextCompressionMiddleware`` / ``compress_transcript`` (3 files).
CONTEXT_COMPRESSION_THRESHOLD: float = 0.70

#: Number of most-recent transcript rounds always kept verbatim (never compacted). Was the default
#: of the same three call-chain sites.
CONTEXT_COMPRESSION_KEEP_RECENT: int = 3

#: Cosine-similarity floor for episodic recall (past runs / reflections) to be surfaced into the
#: prompt. Was the default of both ``recall_similar_runs`` and ``recall_reflections``.
EPISODIC_RECALL_MIN_SIMILARITY: float = 0.35

# --------------------------------------------------------------- product LLM defaults (D-2)
#: Default LLM mode when ``OWCOPILOT_LLM_MODE`` is unset. ``offline`` = the deterministic doubles
#: (test/CI only, itself gated behind ``OWCOPILOT_ALLOW_OFFLINE_LLM``). Was repeated verbatim at the
#: CLI, MCP and service entry points.
DEFAULT_LLM_MODE: str = "offline"

#: Default cheap-tier model id when ``OWCOPILOT_CHEAP_MODEL`` is unset. Was repeated verbatim at the
#: CLI, MCP and service entry points.
DEFAULT_CHEAP_MODEL: str = "deepseek-v4-flash"
