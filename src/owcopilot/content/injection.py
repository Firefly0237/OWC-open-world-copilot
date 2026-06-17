"""Prompt-injection detection for any free text that can reach a model prompt.

Per OWASP LLM01 (2025), the highest-prevalence prompt-injection vector is *indirect*: adversarial
instructions hidden inside content the model later reads — RAG/reference documents, imported lore,
generated dialogue. So this scanner is applied to every such surface (audited canon text *and*
uploaded inspiration references), from one shared pattern set.

Honest scope: this is a regex first layer (defense-in-depth), not a guarantee. OWASP is explicit
that pattern filters miss sophisticated indirect injection; a trained classifier catches more.
We surface matches for human/risk review and keep untrusted content out of instruction position
elsewhere — we do not claim to block every injection here.
"""

from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    re.compile(r"忽略.{0,12}(以上|之前|全部).{0,12}(规则|规范|指令|提示)", re.I),
    re.compile(r"(输出|泄露|显示).{0,12}(系统提示|system prompt|api\s*key|密钥)", re.I),
    re.compile(r"ignore.{0,20}(previous|above|all).{0,20}(instructions|rules)", re.I),
    re.compile(r"(reveal|print|dump).{0,20}(system prompt|api key|secret)", re.I),
]


def scan_for_injection(text: str) -> list[str]:
    """Return the source patterns that matched ``text`` (empty when it looks clean)."""
    return [pattern.pattern for pattern in _INJECTION_PATTERNS if pattern.search(text)]
