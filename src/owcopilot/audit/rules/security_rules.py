"""Security-oriented content checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"忽略.{0,12}(以上|之前|全部).{0,12}(规则|规范|指令|提示)", re.I),
    re.compile(r"(输出|泄露|显示).{0,12}(系统提示|system prompt|api\s*key|密钥)", re.I),
    re.compile(r"ignore.{0,20}(previous|above|all).{0,20}(instructions|rules)", re.I),
    re.compile(r"(reveal|print|dump).{0,20}(system prompt|api key|secret)", re.I),
]


class PromptInjectionRule:
    code = "PROMPT_INJECTION"
    severity = Severity.ERROR
    category = Category.TRUST

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, path, text in _texts(ctx):
            matched = [
                pattern.pattern
                for pattern in _PROMPT_INJECTION_PATTERNS
                if pattern.search(text)
            ]
            if not matched:
                continue
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=target_ref,
                message=f"{target_ref} contains prompt-injection-like instructions",
                evidence=[
                    Evidence(
                        kind="field_path",
                        target_ref=target_ref,
                        path=path,
                        data={"patterns": matched},
                    )
                ],
            )


def _texts(ctx: AuditContext) -> Iterable[tuple[str, str, str]]:
    """Every imported free-text surface that can later reach a prompt via the context pack."""
    for style in ctx.bundle.style_guides.values():
        yield f"style_guide:{style.id}", "body", style.body
    for dialogue in ctx.bundle.dialogues.values():
        if dialogue.text:
            yield f"dialogue:{dialogue.id}", "text", dialogue.text
    for text in ctx.bundle.localized_texts.values():
        yield f"localized_text:{text.id}", "text", text.text
    for term in ctx.bundle.terms.values():
        if term.description:
            yield f"term:{term.id}", "description", term.description
    for entity in ctx.bundle.entities.values():
        if entity.description:
            yield f"entity:{entity.id}", "description", entity.description
    for quest in ctx.bundle.quests.values():
        if quest.title:
            yield f"quest:{quest.id}", "title", quest.title
        if quest.objective:
            yield f"quest:{quest.id}", "objective", quest.objective
    for poi in ctx.bundle.pois.values():
        if poi.purpose:
            yield f"poi:{poi.id}", "purpose", poi.purpose
