# 本地化导出为 XLIFF 1.2（专业互换标准）。
"""Localization export to **XLIFF 1.2** — the professional interchange standard.

CAT tools, TMS systems and translation agencies all speak XLIFF; a localization CSV only carries a
team's in-house pipeline. We emit one ``<file>`` per locale, one ``<trans-unit>`` per string, and
map the native ``ui_max_len`` to XLIFF's standard ``maxwidth``/``size-unit="char"`` attributes so a
translator's CAT tool enforces the UI character budget. The CSV export is kept alongside for simple
spreadsheet workflows; XLIFF is the vendor-facing handoff.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..content.lang import detect_language
from ..content.models import ContentBundle


def _dialogue_tree_units(bundle: ContentBundle) -> list[tuple[str, str, str, int | None]]:
    """(text_key, locale, text, ui_max_len) for every dialogue-tree node/choice that opted in.

    A node/choice has no explicit ``locale`` field (unlike ``LocalizedText``/``DialogueRef``), so a
    unit only appears once BOTH ``text_key`` and ``text`` are set (A1 remediation: this is what
    lets branching dialogue reach the localization pipeline at all), and it is bucketed under its
    detected script/language (:func:`~owcopilot.content.lang.detect_language`) — the same
    "don't ask the author to declare a language" heuristic content ingestion already uses,
    rather than inventing a fixed default locale. Trees with no ``text_key`` (every pre-existing
    tree) contribute nothing here, so old exports are unaffected.
    """
    units: list[tuple[str, str, str, int | None]] = []
    for tree in bundle.dialogue_trees.values():
        for node in tree.nodes.values():
            if node.text_key and node.text:
                units.append((node.text_key, detect_language(node.text).dominant, node.text, None))
            for choice in node.choices:
                if choice.text_key and choice.text:
                    units.append(
                        (choice.text_key, detect_language(choice.text).dominant, choice.text, None)
                    )
    return units


def _all_units(bundle: ContentBundle) -> list[tuple[str, str, str, int | None]]:
    """Every (text_key, locale, text, ui_max_len) translation unit in the bundle.

    Sources: ``localized_texts`` and ``dialogues`` (DialogueRef) carry an explicit ``locale``;
    dialogue tree nodes/choices are added by :func:`_dialogue_tree_units`."""
    units: list[tuple[str, str, str, int | None]] = []
    for text in bundle.localized_texts.values():
        units.append((text.text_key, text.locale, text.text, text.ui_max_len))
    for dialogue in bundle.dialogues.values():
        if dialogue.text is None or not dialogue.locale:
            continue
        units.append((dialogue.text_key, dialogue.locale, dialogue.text, dialogue.ui_max_len))
    units.extend(_dialogue_tree_units(bundle))
    return units


def write_localization_csv(bundle: ContentBundle, path: Path) -> None:
    """Key/Locale/Text/UIMaxLen rows from localized texts, localized dialogue lines and opted-in
    dialogue-tree node/choice text — the simple spreadsheet handoff that sits alongside the XLIFF
    for in-house localization workflows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Key", "Locale", "Text", "UIMaxLen"])
    rows = sorted(
        (key, locale, text, "" if max_len is None else str(max_len))
        for key, locale, text, max_len in _all_units(bundle)
    )
    for row in rows:
        writer.writerow(row)
    path.write_bytes(buffer.getvalue().encode("utf-8"))


def _units(bundle: ContentBundle) -> dict[str, list[tuple[str, str, int | None]]]:
    """Group translatable strings by locale: locale -> [(key, text, ui_max_len)], sorted by key."""
    by_locale: dict[str, list[tuple[str, str, int | None]]] = {}
    for key, locale, text, max_len in _all_units(bundle):
        by_locale.setdefault(locale, []).append((key, text, max_len))
    return {locale: sorted(rows) for locale, rows in by_locale.items()}


def render_xliff(bundle: ContentBundle) -> str:
    by_locale = _units(bundle)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">',
    ]
    for locale in sorted(by_locale):
        # source-language = the locale these strings are written in; a translator sets the target
        # language in their CAT tool and fills <target>. datatype=plaintext is the safe default.
        lines.append(
            f'  <file original="owcopilot" source-language={quoteattr(locale)} '
            f'datatype="plaintext">'
        )
        lines.append("    <body>")
        for key, text, max_len in by_locale[locale]:
            attrs = f"id={quoteattr(key)} resname={quoteattr(key)}"
            if max_len is not None:
                attrs += f' maxwidth="{max_len}" size-unit="char"'
            lines.append(f"      <trans-unit {attrs}>")
            lines.append(f"        <source>{escape(text)}</source>")
            lines.append("      </trans-unit>")
        lines.append("    </body>")
        lines.append("  </file>")
    lines.append("</xliff>")
    return "\n".join(lines) + "\n"


def write_localization_xliff(bundle: ContentBundle, path: Path) -> None:
    path.write_bytes(render_xliff(bundle).encode("utf-8"))
