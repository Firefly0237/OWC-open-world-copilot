"""Security-oriented content checks."""

from __future__ import annotations

from collections.abc import Iterable

from ...content.injection import scan_for_injection
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class PromptInjectionRule:
    code = "PROMPT_INJECTION"
    severity = Severity.ERROR
    category = Category.TRUST

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, path, text in _texts(ctx):
            matched = scan_for_injection(text)
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
        # Item 4: also scan forbidden words and aliases — they are injected verbatim into the
        # [vocabulary-constraints] block, so they carry the same injection risk as descriptions.
        for i, w in enumerate(term.forbidden):
            if w:
                yield f"term:{term.id}", f"forbidden.{i}", w
        for i, a in enumerate(term.aliases):
            if a:
                yield f"term:{term.id}", f"aliases.{i}", a
    for entity in ctx.bundle.entities.values():
        if entity.description:
            yield f"entity:{entity.id}", "description", entity.description
    for quest in ctx.bundle.quests.values():
        if quest.title:
            yield f"quest:{quest.id}", "title", quest.title
        if quest.objective:
            yield f"quest:{quest.id}", "objective", quest.objective
        for index, stage in enumerate(quest.stages):
            if stage.summary:
                yield f"quest:{quest.id}", f"stages.{index}.summary", stage.summary
    for poi in ctx.bundle.pois.values():
        if poi.purpose:
            yield f"poi:{poi.id}", "purpose", poi.purpose
    # Generated dialogue trees reach prompts via dialogue grounding too — scan every node line.
    for tree in ctx.bundle.dialogue_trees.values():
        for node in tree.nodes.values():
            if node.text:
                yield f"dialogue_tree:{tree.id}", f"nodes.{node.id}.text", node.text
