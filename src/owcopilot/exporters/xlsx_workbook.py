# 策划工作台账导出：任务表/台词表/实体表/术语表汇总成一册 .xlsx（opt-in）。
"""Planning workbook export — quests/dialogues/entities/terms as sheets in one ``.xlsx`` (opt-in).

The column vocabulary is **not** invented here: every header is the exact field name
``content/normalize.py`` already reads back on import (``_quest_from_raw`` /
``_dialogue_from_raw`` / ``_entity_from_raw`` / ``_term_from_raw``) — the same names a planner's
CSV/XLSX import already uses. A list-valued field (``tags``/``aliases``/``prerequisites``/…) is
joined with ``;``, the first separator ``normalize._list`` splits on, so a cell copied back into
an import sheet round-trips without reformatting. This keeps the workbook a *view* onto the
existing generic-CSV column contract rather than a second, competing one.

Deterministic and LLM-free (offline, $0), same as the rest of the export path. Row order is by
id (matching ``content_bundle.json``'s ``sort_keys=True`` dump), so re-exporting unchanged content
reproduces the same rows in the same order every time.

openpyxl is imported lazily inside the render function (not at module scope), matching
``content/importers/xlsx.py``'s convention — most CLI invocations never touch a workbook, and this
keeps their import cost out of the common path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..content.models import ContentBundle, DialogueRef, Entity, Quest, Term
from .models import ExportedFile

PLANNING_WORKBOOK_FILENAME = "planning_workbook.xlsx"
PLANNING_WORKBOOK_KIND = "planning_workbook"

_QUEST_COLUMNS = (
    "id",
    "title",
    "giver_npc",
    "location",
    "objective",
    "prerequisites",
    "dialogue_refs",
    "localization_keys",
    "timeline_order",
    "tags",
)
_DIALOGUE_COLUMNS = ("id", "text_key", "speaker_id", "quest_id", "text", "locale", "ui_max_len")
_ENTITY_COLUMNS = ("id", "name", "type", "description", "aliases", "tags", "status", "version")
_TERM_COLUMNS = ("id", "canonical", "aliases", "forbidden", "description")


def _join(values: list[str]) -> str:
    return ";".join(values)


def _quest_row(quest: Quest) -> tuple[Any, ...]:
    return (
        quest.id,
        quest.title,
        quest.giver_npc or "",
        quest.location or "",
        quest.objective,
        _join(quest.prerequisites),
        _join(quest.dialogue_refs),
        _join(quest.localization_keys),
        quest.timeline_order if quest.timeline_order is not None else "",
        _join(quest.tags),
    )


def _dialogue_row(dialogue: DialogueRef) -> tuple[Any, ...]:
    return (
        dialogue.id,
        dialogue.text_key,
        dialogue.speaker_id or "",
        dialogue.quest_id or "",
        dialogue.text or "",
        dialogue.locale or "",
        dialogue.ui_max_len if dialogue.ui_max_len is not None else "",
    )


def _entity_row(entity: Entity) -> tuple[Any, ...]:
    return (
        entity.id,
        entity.name,
        entity.type.value,
        entity.description,
        _join(entity.aliases),
        _join(entity.tags),
        entity.status,
        entity.version or "",
    )


def _term_row(term: Term) -> tuple[Any, ...]:
    return (
        term.id,
        term.canonical,
        _join(term.aliases),
        _join(term.forbidden),
        term.description,
    )


def render_planning_workbook(bundle: ContentBundle) -> bytes:
    """Build the workbook in memory and return its ``.xlsx`` bytes."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font

    header_font = Font(bold=True)
    workbook = Workbook()
    # Workbook() starts with one default sheet ("Sheet"); every sheet below is created explicitly,
    # so the default is removed rather than left behind as an empty fifth tab.
    workbook.remove(workbook.active)

    sheets: tuple[tuple[str, tuple[str, ...], list[tuple[Any, ...]]], ...] = (
        (
            "任务表",
            _QUEST_COLUMNS,
            [_quest_row(q) for q in sorted(bundle.quests.values(), key=lambda q: q.id)],
        ),
        (
            "台词表",
            _DIALOGUE_COLUMNS,
            [_dialogue_row(d) for d in sorted(bundle.dialogues.values(), key=lambda d: d.id)],
        ),
        (
            "实体表",
            _ENTITY_COLUMNS,
            [_entity_row(e) for e in sorted(bundle.entities.values(), key=lambda e: e.id)],
        ),
        (
            "术语表",
            _TERM_COLUMNS,
            [_term_row(t) for t in sorted(bundle.terms.values(), key=lambda t: t.id)],
        ),
    )
    for title, columns, rows in sheets:
        sheet = workbook.create_sheet(title=title)
        sheet.append(columns)
        for cell in sheet[1]:
            cell.font = header_font
        sheet.freeze_panes = "A2"
        for row in rows:
            sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def write_planning_workbook(bundle: ContentBundle, path: str | Path) -> ExportedFile:
    """Write the planning workbook to *path* and return its manifest entry."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render_planning_workbook(bundle))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return ExportedFile(path=output.name, kind=PLANNING_WORKBOOK_KIND, sha256=digest)
