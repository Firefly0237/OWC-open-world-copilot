# 面向安全的内容检查（注入痕迹/越界引用等）。
"""Security-oriented content checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ...content.injection import scan_for_injection
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

# target_ref 的对象类型 -> 策划看得懂的名字；这条规则会扫到内容树里多种自由文本对象。
_KIND_LABELS = {
    "style_guide": "风格指南",
    "dialogue": "对话",
    "localized_text": "本地化文本",
    "term": "术语",
    "entity": "角色/物件",
    "quest": "任务",
    "poi": "地点",
    "dialogue_tree": "对话树",
}


class PromptInjectionRule:
    code = "PROMPT_INJECTION"
    severity = Severity.ERROR
    category = Category.TRUST

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for target_ref, path, text in _texts(ctx):
            matched = scan_for_injection(text)
            if not matched:
                continue
            snippet = _matched_snippet(text, matched)
            kind, _, object_id = target_ref.partition(":")
            label = _KIND_LABELS.get(kind, kind)
            slot = _slot_label(path)
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=target_ref,
                message=(
                    f"{label}「{object_id}」的{slot}里出现了类似“{snippet}”这样的段落,看起来像是"
                    "想让 AI 跳过原本的设定或者交出内部提示词,需要人工确认这是不是故意写进去的"
                    "攻击指令。"
                ),
                evidence=[
                    Evidence(
                        kind="field_path",
                        target_ref=target_ref,
                        path=path,
                        data={"patterns": matched},
                    )
                ],
                suggested_action=(
                    f"需要人工判断:请检查这段文字“{snippet}”是不是有意植入的注入指令。确认是的话"
                    "就删除或改写这一段;如果只是普通剧情用词凑巧撞上了敏感表达,可以保留,但建议换"
                    "一种更安全的写法。"
                ),
            )


def _matched_snippet(text: str, patterns: list[str]) -> str:
    """把命中的正则模式换算回文本里实际出现的那一小段，给策划看得懂的原文而不是正则表达式。"""
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.UNICODE)
        if match:
            return match.group(0)
    return text[:30]


# 证据里的字段路径 -> 策划看得懂的槽位名字（不暴露 body/text/description 这类字段标识符）。
def _slot_label(path: str) -> str:
    if path.startswith("stages"):
        return "阶段摘要"
    if path.startswith("forbidden."):
        return "禁用词列表"
    if path.startswith("aliases."):
        return "别名列表"
    if path.startswith("nodes/"):
        return "节点文本"
    return {
        "body": "正文",
        "text": "文本",
        "description": "简介",
        "purpose": "用途说明",
        "title": "标题",
        "objective": "目标",
    }.get(path, path)


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
                # 用 "/" 而不是 "." 分隔，才能和内容文件里的真实字段路径一致，也才能被
                # ``source_location.node_ref_from_path`` 认出 "nodes/<id>" 从而提取出节点 id。
                yield f"dialogue_tree:{tree.id}", f"nodes/{node.id}/text", node.text
