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

from ..content.models import ContentBundle


def write_localization_csv(bundle: ContentBundle, path: Path) -> None:
    """Key/Locale/Text/UIMaxLen rows from localized texts plus localized dialogue lines — the simple
    spreadsheet handoff that sits alongside the XLIFF for in-house localization workflows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Key", "Locale", "Text", "UIMaxLen"])
    rows: list[tuple[str, str, str, str]] = []
    for text in bundle.localized_texts.values():
        rows.append(
            (
                text.text_key,
                text.locale,
                text.text,
                "" if text.ui_max_len is None else str(text.ui_max_len),
            )
        )
    for dialogue in bundle.dialogues.values():
        if dialogue.text is None or not dialogue.locale:
            continue
        rows.append(
            (
                dialogue.text_key,
                dialogue.locale,
                dialogue.text,
                "" if dialogue.ui_max_len is None else str(dialogue.ui_max_len),
            )
        )
    for row in sorted(rows):
        writer.writerow(row)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _units(bundle: ContentBundle) -> dict[str, list[tuple[str, str, int | None]]]:
    """Group translatable strings by locale: locale -> [(key, text, ui_max_len)], sorted by key."""
    by_locale: dict[str, list[tuple[str, str, int | None]]] = {}
    for text in bundle.localized_texts.values():
        by_locale.setdefault(text.locale, []).append((text.text_key, text.text, text.ui_max_len))
    for dialogue in bundle.dialogues.values():
        if dialogue.text is None or not dialogue.locale:
            continue
        by_locale.setdefault(dialogue.locale, []).append(
            (dialogue.text_key, dialogue.text, dialogue.ui_max_len)
        )
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
    path.write_text(render_xliff(bundle), encoding="utf-8")
