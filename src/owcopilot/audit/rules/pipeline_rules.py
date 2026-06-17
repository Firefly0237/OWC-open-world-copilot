"""Production pipeline rules: localization, UI text and terminology checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_PLACEHOLDER_RE = re.compile(r"\{(?:[a-zA-Z_][a-zA-Z0-9_]*|\d+)\}")


class MissingLocalizationKeyRule:
    code = "MISSING_LOCALIZATION_KEY"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for quest in ctx.bundle.quests.values():
            if not quest.localization_keys:
                target_ref = f"quest:{quest.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"Quest '{quest.id}' has no localization keys",
                    evidence=[
                        Evidence(kind="field_path", target_ref=target_ref, path="localization_keys")
                    ],
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
                    message=f"Quest '{quest.id}' has no objective",
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="objective")],
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
                        f"Dialogue '{dialogue.id}' text length {len(text)} exceeds UI limit {limit}"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="text")],
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
                        f"Localized text '{text_obj.id}' length {len(text_obj.text)} exceeds "
                        f"UI limit {text_limit}"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="text")],
                )


class PlaceholderMismatchRule:
    code = "PLACEHOLDER_MISMATCH"
    severity = Severity.ERROR
    category = Category.PIPELINE

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        by_key: dict[str, dict[str, set[str]]] = {}
        for dialogue in ctx.bundle.dialogues.values():
            if not dialogue.locale or dialogue.text is None:
                continue
            by_key.setdefault(dialogue.text_key, {})[dialogue.locale] = _placeholders(dialogue.text)
        for text in ctx.bundle.localized_texts.values():
            by_key.setdefault(text.text_key, {})[text.locale] = _placeholders(text.text)

        for text_key, locale_map in by_key.items():
            if len(locale_map) < 2:
                continue
            expected = next(iter(locale_map.values()))
            for locale, placeholders in locale_map.items():
                if placeholders != expected:
                    target_ref = f"dialogue_key:{text_key}"
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"Dialogue key '{text_key}' placeholder set in locale '{locale}' "
                            f"differs from other locales"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=f"locales.{locale}.text",
                                data={
                                    "expected": sorted(expected),
                                    "actual": sorted(placeholders),
                                },
                            )
                        ],
                    )


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
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=target_ref,
                        message=(
                            f"{target_ref} uses forbidden term '{forbidden}', "
                            f"use canonical term '{canonical}'"
                        ),
                        evidence=[
                            Evidence(
                                kind="field_path",
                                target_ref=target_ref,
                                path=path,
                                data={"forbidden": forbidden, "canonical": canonical},
                            )
                        ],
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
