"""Prompt-injection detection for any free text that can reach a model prompt.

Per OWASP LLM01 (2025), the highest-prevalence prompt-injection vector is *indirect*: adversarial
instructions hidden inside content the model later reads — RAG/reference documents, imported lore,
generated dialogue. So this scanner is applied to every such surface (audited canon text *and*
uploaded inspiration references), from one shared pattern set.

Honest scope: this is a regex first layer (defense-in-depth), not a guarantee. OWASP is explicit
that pattern filters miss sophisticated indirect injection; a trained classifier catches more.
We surface matches for human/risk review and keep untrusted content out of instruction position
elsewhere — we do not claim to block every injection here.

Where the real defense lives: these regexes are a best-effort *signal layer*, NOT the primary
injection defense. The authoritative defense is architectural — privilege isolation plus
human-in-the-loop. LLM output is NEVER auto-written to canon: every AI draft only enters the
``ReviewQueue`` (``assist/review_queue.py``, status ``pending_review``), and the one and only
path that materialises a draft into the content store is
``pipeline/review.py::decide_review_item`` — which requires a named operator to accept it and
must clear the deterministic audit gate (``_assert_no_new_accept_errors``). So even if an
injection string slips past these patterns, it can only sit in the review queue for a human to
decide; it cannot silently mutate canon or reach an instruction position on its own.
"""

from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    # ── Original patterns (re.UNICODE added for full-width/homoglyph coverage) ─────────────────
    re.compile(r"忽略.{0,12}(以上|之前|全部).{0,12}(规则|规范|指令|提示)", re.I | re.UNICODE),
    re.compile(
        r"(输出|泄露|显示).{0,12}(系统提示|system prompt|api\s*key|密钥)", re.I | re.UNICODE
    ),
    re.compile(r"ignore.{0,20}(previous|above|all).{0,20}(instructions|rules)", re.I | re.UNICODE),
    re.compile(r"(reveal|print|dump).{0,20}(system prompt|api key|secret)", re.I | re.UNICODE),
    # ── Item 5: Chinese synonym expansions ────────────────────────────────────────────────────
    # "忘记/无视/清空/丢弃 … 以上/之前/以前/全部 … 规则/指令/提示"
    re.compile(
        r"(忘记|无视|清空|丢弃|清除|抛弃).{0,20}(以上|之前|以前|全部|所有).{0,20}(规则|规范|指令|提示|设定)",
        re.I | re.UNICODE,
    ),
    # "你的/系统/真实/原本 … 指令/提示/设定/角色"
    re.compile(
        r"(你的|系统|真实|原本|实际).{0,8}(指令|提示|设定|角色|任务|身份)",
        re.I | re.UNICODE,
    ),
    # "复述/展示/泄露/打印/输出 … 以上/之前/系统 … 内容/指令/提示"
    re.compile(
        r"(复述|展示|打印|输出|泄露).{0,12}(以上|之前|系统|初始).{0,12}(内容|指令|提示|设定)",
        re.I | re.UNICODE,
    ),
    # "扮演 … 没有限制/无限制/自由/不受约束"
    re.compile(
        r"扮演.{0,15}(没有限制|无限制|不受限|自由|无约束|不受约束)",
        re.I | re.UNICODE,
    ),
    # ── Item 5: English synonym expansions ───────────────────────────────────────────────────
    # "disregard/forget/skip … [all] [your] previous/above … instructions/rules/constraints"
    # Allow 0–2 modifier words (all, your, the) before the anchor word so that multi-word
    # chains like "all your previous" are caught (OWASP LLM01 classic pattern).
    re.compile(
        r"(disregard|forget|skip|overlook|bypass)\s+((?:all|your|the)\s+){0,2}"
        r"(previous|above|prior|earlier|your)\s+"
        r"(instructions?|rules?|constraints?|directives?|guidelines?)",
        re.I | re.UNICODE,
    ),
    # "show/tell/reveal/print/output/repeat … [me] [your] … system prompt / instructions …"
    re.compile(
        r"(show|tell|reveal|print|output|repeat|display)\s+(me\s+|your\s+)?"
        r"(your\s+|initial\s+|the\s+)?"
        r"(system\s+prompt|instructions?|configuration|directives?|constraints?)",
        re.I | re.UNICODE,
    ),
    # "act as if you have no / without any … rules/restrictions/constraints"
    re.compile(
        r"act\s+as\s+(if\s+you\s+have\s+no|without\s+any)\s+(rules?|restrictions?|constraints?|limits?)",
        re.I | re.UNICODE,
    ),
    # Newline-separated instruction injection: lone "---" or "===" or ">>>" used to demarcate a
    # new instruction block.  Only flag when immediately followed by a command-like English phrase.
    re.compile(
        r"[-=*>#]{3,}\s*(ignore|forget|disregard|reveal|act\s+as|you\s+are)",
        re.I | re.UNICODE,
    ),
]


def scan_for_injection(text: str) -> list[str]:
    """Return the source patterns that matched ``text`` (empty when it looks clean).

    This is the signal layer; the authoritative defense is the human-review write boundary
    (LLM output never auto-writes canon — see ``pipeline/review.py::decide_review_item``).
    """
    return [pattern.pattern for pattern in _INJECTION_PATTERNS if pattern.search(text)]
