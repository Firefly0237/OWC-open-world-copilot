# 策划笔记导出：世界概览 + 任务卡 + 人物设定卡 + 可选审查报告 → 一份 .docx（opt-in）。
"""Planning-notes export — a single ``.docx`` a planner opens instead of the raw JSON bundle.

Four sections, in order: 世界概览 (headline counts + regions), 任务卡 (one card per quest),
人物设定卡 (one card per NPC entity), and an optional 审查报告 section when the caller supplies
audit findings.
Built with **python-docx** (a pure-Python dependency, no LibreOffice/Word install required) rather
than the hand-rolled minimal OOXML writer in ``exporters/lorebook.py``: that writer only emits bold
runs of varying size, no real heading *styles*, no tables and no table of contents, all of which
this format needs (标题层级/表格/目录 — see the batch-8 node). ``exporters/lorebook.py`` stays as-is
for its own (different) audience — a flowing prose compendium for novelists/GMs, not a planner's
working notes.

The review-findings section reuses ``AuditFindingInput`` — the SAME exporter-facing findings
vocabulary ``exporters/unreal_datatable.py`` already established for its ``AuditFindings`` table —
instead of inventing a second one. An empty/omitted ``review_findings`` iterable simply omits the
section, so every caller that has no audit result handy still gets a complete three-section note.

Deterministic and LLM-free (offline, $0): python-docx does not stamp a wall-clock timestamp into
``docProps/core.xml`` the way openpyxl does, so re-rendering unchanged input reproduces the same
bytes.

The ``Document``/``Paragraph`` helper parameters below are typed ``Any`` rather than importing
python-docx's classes: the package ships no inline type information, so a precise annotation would
buy nothing and mypy would treat it as ``Any`` either way (``ignore_missing_imports`` in
``pyproject.toml``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..content.models import ContentBundle, Entity, EntityType, Quest
from .models import ExportedFile
from .unreal_datatable import AuditFindingInput

PLANNING_NOTES_FILENAME = "planning_notes.docx"
PLANNING_NOTES_KIND = "planning_notes"

_TOC_FIELD = r'TOC \o "1-3" \h \z \u'
_TOC_PLACEHOLDER = "（打开 Word 后右键点击目录，选择“更新域”即可生成/刷新页码）"


def _add_toc(document: Any) -> None:
    """Insert a ``TOC`` field so Word regenerates a real table of contents on open/update.

    python-docx has no high-level TOC API; a field code is the documented mechanism Word itself
    uses (Insert ▸ Table of Contents writes exactly this field). We ship the field plus a visible
    placeholder run so a reader who never updates fields still sees a note instead of a blank line.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = _TOC_FIELD
    run._r.append(begin)
    run._r.append(instr)
    for kind, text in (("separate", None), ("text", _TOC_PLACEHOLDER), ("end", None)):
        wrapper = OxmlElement("w:r")
        if kind == "text":
            node = OxmlElement("w:t")
            node.text = text
        else:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        wrapper.append(node)
        paragraph._p.append(wrapper)


def _kv_table(document: Any, rows: list[tuple[str, str]]) -> None:
    """A simple 字段/内容 two-column table; skips entirely when every value is empty."""
    if not any(value for _, value in rows):
        return
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for key, value in rows:
        if not value:
            continue
        cells = table.add_row().cells
        cells[0].text = key
        cells[1].text = value


def _world_overview_section(document: Any, bundle: ContentBundle) -> None:
    document.add_heading("世界概览", level=1)
    document.add_paragraph(
        f"本次导出包含 {len(bundle.entities)} 个实体、{len(bundle.quests)} 个任务、"
        f"{len(bundle.dialogues)} 条台词、{len(bundle.dialogue_trees)} 棵对白树、"
        f"{len(bundle.terms)} 个术语、{len(bundle.localized_texts)} 条本地化词条。"
    )
    regions = sorted(bundle.regions.values(), key=lambda r: r.id)
    if regions:
        table = document.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        header = table.rows[0].cells
        header[0].text = "区域"
        header[1].text = "主题"
        for region in regions:
            cells = table.add_row().cells
            cells[0].text = region.name
            cells[1].text = "、".join(region.themes)


def _quest_card(document: Any, quest: Quest) -> None:
    document.add_heading(f"{quest.title}（{quest.id}）", level=2)
    _kv_table(
        document,
        [
            ("目标", quest.objective),
            ("发布人", quest.giver_npc or ""),
            ("地点", quest.location or ""),
            ("前置任务", "；".join(quest.prerequisites)),
            ("关联对白", "；".join(quest.dialogue_refs)),
            ("标签", "、".join(quest.tags)),
            (
                "时间线顺序",
                "" if quest.timeline_order is None else str(quest.timeline_order),
            ),
        ],
    )
    if quest.stages:
        document.add_paragraph("阶段：")
        for stage in quest.stages:
            document.add_paragraph(stage.summary, style="List Bullet")
    if quest.rewards:
        rewards = "、".join(
            f"{r.kind}×{r.amount}" if r.amount is not None else r.kind for r in quest.rewards
        )
        document.add_paragraph(f"奖励：{rewards}")


def _quest_cards_section(document: Any, bundle: ContentBundle) -> None:
    document.add_heading("任务卡", level=1)
    quests = sorted(bundle.quests.values(), key=lambda q: q.id)
    if not quests:
        document.add_paragraph("本世界暂无任务。")
        return
    for quest in quests:
        _quest_card(document, quest)


def _character_card(document: Any, entity: Entity) -> None:
    alias = f"（{'、'.join(entity.aliases)}）" if entity.aliases else ""
    document.add_heading(f"{entity.name}{alias}", level=2)
    _kv_table(
        document,
        [
            ("状态", entity.status),
            ("标签", "、".join(entity.tags)),
            ("版本", entity.version or ""),
        ],
    )
    if entity.description:
        document.add_paragraph(entity.description)


def _character_cards_section(document: Any, bundle: ContentBundle) -> None:
    document.add_heading("人物设定卡", level=1)
    npcs = sorted(
        (e for e in bundle.entities.values() if e.type is EntityType.NPC), key=lambda e: e.id
    )
    if not npcs:
        document.add_paragraph("本世界暂无 NPC 人物条目。")
        return
    for entity in npcs:
        _character_card(document, entity)


def _review_section(document: Any, findings: Iterable[AuditFindingInput]) -> None:
    rows = list(findings)
    if not rows:
        return
    document.add_heading("审查报告", level=1)
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].text = "规则"
    header[1].text = "严重级别"
    header[2].text = "对象"
    header[3].text = "说明"
    for finding in rows:
        cells = table.add_row().cells
        cells[0].text = finding.rule_code
        cells[1].text = finding.severity
        cells[2].text = finding.target_ref
        cells[3].text = finding.message


def render_planning_notes(
    bundle: ContentBundle,
    *,
    review_findings: Iterable[AuditFindingInput] = (),
) -> bytes:
    """Build the planning-notes document in memory and return its ``.docx`` bytes."""
    import io

    import docx

    document = docx.Document()
    document.add_heading("策划笔记", level=0)
    _add_toc(document)
    _world_overview_section(document, bundle)
    _quest_cards_section(document, bundle)
    _character_cards_section(document, bundle)
    _review_section(document, review_findings)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def write_planning_notes(
    bundle: ContentBundle,
    path: str | Path,
    *,
    review_findings: Iterable[AuditFindingInput] = (),
) -> ExportedFile:
    """Write the planning-notes document to *path* and return its manifest entry."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render_planning_notes(bundle, review_findings=review_findings))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return ExportedFile(path=output.name, kind=PLANNING_NOTES_KIND, sha256=digest)
