"""Lore-book export: a human-readable world compendium (Markdown + minimal .docx).

For the broad creator audience (novelists, GMs, hobbyist worldbuilders) the deliverable is
a readable document, not an engine table. The .docx writer emits a minimal valid OOXML
package with the standard library only — same zero-dependency stance as the .docx reader.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from ..content.models import ContentBundle, EntityType

_TYPE_ORDER = [
    (EntityType.FACTION, "势力"),
    (EntityType.NPC, "角色"),
    (EntityType.LOCATION, "地点"),
    (EntityType.ITEM, "物品"),
    (EntityType.SKILL, "技能"),
    (EntityType.ACHIEVEMENT, "成就"),
    (EntityType.EVENT, "事件"),
    (EntityType.ORGANIZATION, "组织"),
    (EntityType.CONCEPT, "概念"),
]


def render_lorebook_markdown(bundle: ContentBundle, *, title: str = "世界设定集") -> str:
    lines: list[str] = [f"# {title}", ""]
    style = bundle.style_guides.get("style_guide")
    if style is not None and (style.body or style.rules):
        lines += ["## 风格圣经", ""]
        if style.body:
            lines += [style.body.strip(), ""]
        for rule in style.rules:
            lines.append(f"- {rule}")
        if style.rules:
            lines.append("")

    for entity_type, label in _TYPE_ORDER:
        rows = sorted(
            (e for e in bundle.entities.values() if e.type is entity_type),
            key=lambda e: e.id,
        )
        if not rows:
            continue
        lines += [f"## {label}", ""]
        for entity in rows:
            alias = f"（{('、'.join(entity.aliases))}）" if entity.aliases else ""
            lines.append(f"### {entity.name}{alias}")
            if entity.description:
                lines.append(entity.description.strip())
            related = [rel for rel in bundle.relations if entity.id in (rel.source, rel.target)]
            if related:
                names = {e.id: e.name for e in bundle.entities.values()}
                rel_lines = []
                for rel in related[:12]:
                    source = names.get(rel.source, rel.source)
                    target = names.get(rel.target, rel.target)
                    rel_lines.append(f"{source} —{rel.kind}→ {target}")
                lines.append("关系：" + "；".join(rel_lines))
            lines.append("")

    regions = sorted(bundle.regions.values(), key=lambda r: r.id)
    if regions:
        lines += ["## 区域", ""]
        for region in regions:
            level = ""
            if region.level_min is not None or region.level_max is not None:
                level = f"（等级 {region.level_min or '?'}–{region.level_max or '?'}）"
            lines.append(f"### {region.name}{level}")
            if region.themes:
                lines.append("主题：" + "、".join(region.themes))
            if region.banned_content:
                lines.append("禁入内容：" + "、".join(region.banned_content))
            lines.append("")

    quests = sorted(
        bundle.quests.values(),
        key=lambda q: (q.timeline_order if q.timeline_order is not None else 10**9, q.id),
    )
    if quests:
        lines += ["## 任务年表", ""]
        for quest in quests:
            order = f"{quest.timeline_order}. " if quest.timeline_order is not None else ""
            lines.append(f"### {order}{quest.title}")
            if quest.objective:
                lines.append(quest.objective.strip())
            for stage in quest.stages:
                lines.append(f"- {stage.summary}")
            lines.append("")

    terms = sorted(bundle.terms.values(), key=lambda t: t.id)
    if terms:
        lines += ["## 术语表", ""]
        for term in terms:
            alias = f"（{('、'.join(term.aliases))}）" if term.aliases else ""
            forbidden = f" ｜ 禁用：{('、'.join(term.forbidden))}" if term.forbidden else ""
            description = f"：{term.description}" if term.description else ""
            lines.append(f"- **{term.canonical}**{alias}{description}{forbidden}")
        lines.append("")

    trees = sorted(bundle.dialogue_trees.values(), key=lambda t: t.id)
    if trees:
        lines += ["## 对话树清单", ""]
        for tree in trees:
            lines.append(f"- **{tree.title or tree.id}**（{len(tree.nodes)} 节点）")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_lorebook(
    bundle: ContentBundle,
    output_dir: str | Path,
    *,
    title: str = "世界设定集",
    formats: tuple[str, ...] = ("md", "docx"),
) -> list[dict[str, str]]:
    """Write the lore book in the requested formats; returns manifest rows with sha256."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    markdown = render_lorebook_markdown(bundle, title=title)
    files: list[dict[str, str]] = []
    if "md" in formats:
        path = output / "lorebook.md"
        path.write_text(markdown, encoding="utf-8")
        files.append(_manifest_row(output, "lorebook.md", "lorebook_markdown"))
    if "docx" in formats:
        path = output / "lorebook.docx"
        _write_docx(markdown, path)
        files.append(_manifest_row(output, "lorebook.docx", "lorebook_docx"))
    return files


# --------------------------------------------------------------------- minimal OOXML writer
_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _write_docx(markdown: str, path: Path) -> None:
    paragraphs: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            paragraphs.append(_para(line[4:], bold=True, size=26))
        elif line.startswith("## "):
            paragraphs.append(_para(line[3:], bold=True, size=32))
        elif line.startswith("# "):
            paragraphs.append(_para(line[2:], bold=True, size=40))
        elif line.startswith("- "):
            paragraphs.append(_para("· " + line[2:].replace("**", "")))
        else:
            paragraphs.append(_para(line.replace("**", "")))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS}"><w:body>' + "".join(paragraphs) + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", document)


def _para(text: str, *, bold: bool = False, size: int | None = None) -> str:
    props = ""
    if bold or size is not None:
        bold_tag = "<w:b/>" if bold else ""
        size_tag = f'<w:sz w:val="{size}"/>' if size is not None else ""
        props = f"<w:rPr>{bold_tag}{size_tag}</w:rPr>"
    return f'<w:p><w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _manifest_row(output: Path, relative: str, kind: str) -> dict[str, str]:
    digest = hashlib.sha256((output / relative).read_bytes()).hexdigest()
    return {"path": relative, "kind": kind, "sha256": digest}
