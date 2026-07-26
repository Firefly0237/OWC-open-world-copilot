# 生产流水线规则：本地化/UI 文本/术语一致性检查。
"""Production pipeline rules: localization, UI text and terminology checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_PLACEHOLDER_RE = re.compile(r"\{(?:[a-zA-Z_][a-zA-Z0-9_]*|\d+)\}")

# 任务在消息里给策划看的标签：优先带标题，没有标题时才退回只报编号（多条规则要用到）。
def _quest_label(quest_id: str, title: str) -> str:
    return f"任务《{title}》(编号 {quest_id})" if title else f"编号为 {quest_id} 的任务"


class MissingLocalizationKeyRule:
    code = "MISSING_LOCALIZATION_KEY"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if not quest.localization_keys:
                target_ref = f"quest:{quest.id}"
                key = f"quest.{quest.id}.objective"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"{_quest_label(quest.id, quest.title)}还没有配置本地化键,进不了本地化流程,"
                        "上线后这个任务的文本可能显示不出来,或者玩家只能看到英文原文。"
                    ),
                    evidence=[
                        Evidence(kind="field_path", target_ref=target_ref, path="localization_keys")
                    ],
                    suggested_action=(
                        f"在这个任务的本地化键列表里加一条 `{key}`,让它能进入本地化流程。"
                    ),
                )


class QuestMissingObjectiveRule:
    code = "QUEST_MISSING_OBJECTIVE"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if not quest.objective.strip():
                target_ref = f"quest:{quest.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"{_quest_label(quest.id, quest.title)}的目标字段是空的,策划和玩家都不清楚"
                        "这个任务具体要做什么。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="objective")],
                    suggested_action=(
                        f"需要人工判断:请在{_quest_label(quest.id, quest.title)}的目标字段里写清楚"
                        "玩家具体要做什么(比如击败谁、收集什么、护送谁去哪里)——这段话需要策划创作,"
                        "不能靠机器自动编造。"
                    ),
                )


class TextTooLongForUIRule:
    code = "TEXT_TOO_LONG_FOR_UI"
    severity = Severity.WARNING
    category = Category.PIPELINE

    def __init__(self, *, max_chars: int = 80) -> None:
        self.max_chars = max_chars

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for dialogue in ctx.bundle.dialogues.values():
            text = dialogue.text or ""
            metadata_limit = _int(dialogue.metadata.get("ui_max_len"))
            limit = dialogue.ui_max_len or metadata_limit or self.max_chars
            if len(text) > limit:
                target_ref = f"dialogue:{dialogue.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"对话《{dialogue.id}》的文本共 {len(text)} 个字,超过了这个界面能显示的 "
                        f"{limit} 个字上限,玩家在界面里可能看到文字被截断或者溢出。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="text")],
                    suggested_action=(
                        f"把这段对话文本从 {len(text)} 字精简到 {limit} 字以内;如果这段话必须保留"
                        f"完整信息,就找程序把这条对话允许的界面字数上限从 {limit} 调大。"
                    ),
                )
        for text_obj in ctx.bundle.localized_texts.values():
            text_limit = text_obj.ui_max_len or _int(text_obj.metadata.get("ui_max_len"))
            if text_limit is None:
                continue
            if len(text_obj.text) > text_limit:
                target_ref = f"localized_text:{text_obj.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"本地化文本条目《{text_obj.id}》共 {len(text_obj.text)} 个字,超过了这个"
                        f"界面能显示的 {text_limit} 个字上限,玩家在界面里可能看到文字被截断或溢出。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="text")],
                    suggested_action=(
                        f"把这条文本从 {len(text_obj.text)} 字精简到 {text_limit} 字以内;如果必须"
                        f"保留完整信息,就找程序把这条文本允许的界面字数上限从 {text_limit} 调大。"
                    ),
                )


class PlaceholderMismatchRule:
    code = "PLACEHOLDER_MISMATCH"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        # 值是 (占位符集合, 这条文本所属对象的 ref)。带上对象 ref 是因为一个文本键横跨多个语言
        # 版本、散在多个对象里，光有键定位不到任何一个文件；带上它，报告才能说出"去哪个文件改"。
        by_key: dict[str, dict[str, tuple[set[str], str]]] = {}
        for dialogue in ctx.bundle.dialogues.values():
            if not dialogue.locale or dialogue.text is None:
                continue
            by_key.setdefault(dialogue.text_key, {})[dialogue.locale] = (
                _placeholders(dialogue.text),
                f"dialogue:{dialogue.id}",
            )
        for text in ctx.bundle.localized_texts.values():
            by_key.setdefault(text.text_key, {})[text.locale] = (
                _placeholders(text.text),
                f"localized_text:{text.id}",
            )

        for text_key, locale_map in by_key.items():
            if len(locale_map) < 2:
                continue
            expected = next(iter(locale_map.values()))[0]
            for locale, (placeholders, owner_ref) in locale_map.items():
                if placeholders != expected:
                    target_ref = f"dialogue_key:{text_key}"
                    expected_sorted = sorted(expected)
                    actual_sorted = sorted(placeholders)
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"文本键『{text_key}』的『{locale}』语言版本里,携带的可替换占位符是 "
                            f"{actual_sorted},跟其他语言版本里的 {expected_sorted} 不一样,替换变量"
                            "时这一条会跟别的语言对不上,可能漏内容甚至直接报错。"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"locales.{locale}.text",
                                data={
                                    "expected": expected_sorted,
                                    "actual": actual_sorted,
                                },
                            ),
                            # 这一条真正落在哪个对象上：定位器据此推出文件与行号。
                            Evidence(kind="field_path", target_ref=owner_ref, path="text"),
                        ],
                        suggested_action=(
                            f"把『{locale}』语言版本文本里的占位符改成 {expected_sorted},和其它语言"
                            f"版本保持一致(现在是 {actual_sorted})。"
                        ),
                    )


# target_ref 的对象类型 -> 策划看得懂的名字；下面两条规则（TermInconsistentRule / PromptInjection
# 由 security_rules.py 单独维护自己的一份，这里只覆盖 _texts() 会产出的三种）。
_KIND_LABELS = {"quest": "任务", "dialogue": "对话", "localized_text": "本地化文本"}
_SLOT_LABELS = {"title": "标题", "objective": "目标", "text": "文本"}


class TermInconsistentRule:
    code = "TERM_INCONSISTENT"
    severity = Severity.WARNING
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        forbidden_terms = {
            forbidden.lower(): term.canonical
            for term in ctx.bundle.terms.values()
            for forbidden in term.forbidden
        }
        if not forbidden_terms:
            return
        for target_ref, path, text in _texts(ctx):
            lowered = text.lower()
            for forbidden, canonical in forbidden_terms.items():
                if _term_appears(forbidden, lowered):
                    kind, _, object_id = target_ref.partition(":")
                    label = _KIND_LABELS.get(kind, kind)
                    slot = _SLOT_LABELS.get(path, path)
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"{label}「{object_id}」的{slot}里用了禁用词『{forbidden}』,按术语表"
                            f"应该改用标准称呼『{canonical}』,统一后能避免玩家看到两套不同的叫法。"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=path,
                                data={"forbidden": forbidden, "canonical": canonical},
                            )
                        ],
                        suggested_action=(
                            f"把这段文字里的『{forbidden}』替换成标准称呼『{canonical}』。"
                        ),
                    )


_LATIN_TERM_RE = re.compile(r"[a-z0-9][a-z0-9 _-]*")


def _term_appears(forbidden: str, lowered: str) -> bool:
    """Whether ``forbidden`` occurs in ``lowered`` as a term, not a coincidental substring.

    A Latin term needs word boundaries so a forbidden 'war' does not flag 'warden' or 'toward'
    (a false positive). CJK has no word boundaries, so a substring match is the only option there
    and is acceptable because CJK terms are seldom substrings of unrelated words."""
    if _LATIN_TERM_RE.fullmatch(forbidden):
        return re.search(rf"\b{re.escape(forbidden)}\b", lowered) is not None
    return forbidden in lowered


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


def _texts(ctx: AuditContext) -> Iterable[tuple[str, str, str]]:
    for quest in ctx.bundle.quests.values():
        if quest.title:
            yield f"quest:{quest.id}", "title", quest.title
        if quest.objective:
            yield f"quest:{quest.id}", "objective", quest.objective
    for dialogue in ctx.bundle.dialogues.values():
        if dialogue.text:
            yield f"dialogue:{dialogue.id}", "text", dialogue.text
    for text in ctx.bundle.localized_texts.values():
        yield f"localized_text:{text.id}", "text", text.text


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None
