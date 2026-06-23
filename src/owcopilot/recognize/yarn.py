"""Recognize narrative structure from Yarn Spinner scripts (.yarn).

Yarn's structure is explicit enough to extract deterministically: ``title:``-delimited nodes, the
``<<jump>>`` / ``[[option|Target]]`` edges between them, ``<<declare>>``-d variables, and the
``Speaker:`` prefix on dialogue lines (which Yarn treats as a real character marker). So we lift
nodes + flow edges + variables + speaker→node relations without a model. Deeper character-relation
inference from the dialogue prose is the optional §8-guarded LLM pass, not guessed here.
"""

from __future__ import annotations

import re

from .models import ImportPlan, ProposedEntity, ProposedRelation, SourceRef

_JUMP = re.compile(r"<<\s*(?:jump|detour)\s+([A-Za-z_]\w*)\s*>>")
# [[text|Target]] or [[Target]] option links
_OPTION_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?\s*([A-Za-z_]\w*)\s*\]\]")
_DECLARE = re.compile(r"<<\s*declare\s+\$([A-Za-z_]\w*)\s*(?:=|to)\s*(.+?)\s*>>")
# A dialogue line "Speaker: text" — speaker is a bare word; commands (<<...>>) and options aren't.
_SPEAKER = re.compile(r"^\s*([A-Za-z_][\w ]*?)\s*:\s*\S")


def recognize_yarn(text: str, *, source_file: str = "") -> ImportPlan:
    """Extract nodes, jump/option edges, declared variables, and speaker→node relations."""
    entities: list[ProposedEntity] = []
    node_ids: set[str] = set()
    character_ids: set[str] = set()
    relations: list[ProposedRelation] = []
    variables: list[dict[str, object]] = []
    warnings: list[str] = []
    raw_edges: list[tuple[str, str, str, int]] = []  # (node, target, evidence, line_no)

    def add_node(node_id: str, line_no: int) -> None:
        if node_id in node_ids:
            return
        node_ids.add(node_id)
        entities.append(
            ProposedEntity(
                id=node_id,
                name=node_id,
                type="event",
                source_ref=SourceRef(file=source_file, locator=f"yarn:{node_id}@L{line_no}"),
            )
        )

    def add_character(name: str, node_id: str, line_no: int) -> None:
        if name not in character_ids:
            character_ids.add(name)
            loc = f"yarn:speaker:{name}@L{line_no}"
            entities.append(
                ProposedEntity(
                    id=name,
                    name=name,
                    type="npc",
                    source_ref=SourceRef(file=source_file, locator=loc),
                )
            )
        relations.append(
            ProposedRelation(
                source=name,
                target=node_id,
                kind="speaks_in",
                evidence=f"{name}:",
                source_ref=SourceRef(file=source_file, locator=f"yarn:{name}@{node_id}@L{line_no}"),
            )
        )

    in_body = False
    current: str | None = None
    pending_title: str | None = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped == "---":  # header → body
            in_body = True
            if pending_title:
                current = pending_title
                add_node(current, line_no)
                pending_title = None
            continue
        if stripped == "===":  # end of node
            in_body, current = False, None
            continue
        if not in_body:  # header region (or between nodes)
            if stripped.lower().startswith("title:"):
                pending_title = stripped.split(":", 1)[1].strip()
            continue

        # --- body line ---
        for decl in _DECLARE.finditer(raw):
            variables.append({"name": decl.group(1), "value": decl.group(2)})
        if current is None:
            continue
        for target in _JUMP.findall(raw):
            raw_edges.append((current, target, f"<<jump {target}>>", line_no))
        for target in _OPTION_LINK.findall(raw):
            raw_edges.append((current, target, f"[[…|{target}]]", line_no))
        if "<<" not in stripped and "[[" not in stripped:
            speaker = _SPEAKER.match(raw)
            if speaker:
                add_character(speaker.group(1).strip(), current, line_no)

    for node, target, evidence, line_no in raw_edges:
        if target not in node_ids:
            warnings.append(f"L{line_no}: 跳转 `{evidence}` 指向未识别节点「{target}」，保留待人审")
            continue
        relations.append(
            ProposedRelation(
                source=node,
                target=target,
                kind="leads_to",
                evidence=evidence,
                source_ref=SourceRef(file=source_file, locator=f"yarn:{node}->{target}@L{line_no}"),
            )
        )

    return ImportPlan(
        source_format="yarn",
        entities=entities,
        relations=relations,
        variables=variables,
        warnings=warnings,
    )
