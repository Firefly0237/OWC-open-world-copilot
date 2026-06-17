"""Recognize narrative structure from an ink script (inkle's scripting language).

ink is text-first — most entities are *implicit* mentions, so the safe deterministic signal is the
flow skeleton: knots/stitches as narrative nodes, ``-> divert`` as edges between them, and declared
variables. We extract exactly that and leave character/faction inference (from dialogue prose) to
the optional §8-guarded LLM pass — never guessing here. This is "best-effort + honestly mark what we
did not cover", not a full ink runtime (LISTs, tunnels, threads, weave are noted, not executed).
"""

from __future__ import annotations

import re

from .models import ImportPlan, ProposedEntity, ProposedRelation, SourceRef

# === knot ===  /  == knot  (any number of '='); capture first identifier, skip `function` knots.
_KNOT = re.compile(r"^\s*={2,}\s*(function\s+)?([A-Za-z_]\w*)")
# = stitch  (exactly one leading '=')
_STITCH = re.compile(r"^\s*=(?!=)\s*([A-Za-z_]\w*)")
# -> target  (divert); target is an identifier, possibly dotted (knot.stitch). Stops at params/glue.
_DIVERT = re.compile(r"->\s*([A-Za-z_][\w.]*)")
_VAR = re.compile(r"^\s*(VAR|CONST|LIST)\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_COMMENT = re.compile(r"//.*$")
# Divert targets that are control flow, not nodes:
_SPECIAL_TARGETS = {"END", "DONE", "->", "ref"}


def _strip_comment(line: str) -> str:
    return _COMMENT.sub("", line)


def recognize_ink(text: str, *, source_file: str = "") -> ImportPlan:
    """Extract knots/stitches as nodes, diverts as ``leads_to`` edges, and declared variables."""
    entities: list[ProposedEntity] = []
    node_ids: set[str] = set()
    raw_diverts: list[tuple[str, str, int]] = []  # (container_id, target, line_no)
    variables: list[dict[str, object]] = []
    warnings: list[str] = []

    current_knot: str | None = None
    container: str | None = None  # knot or knot.stitch
    in_block_comment = False

    def add_node(node_id: str, line_no: int) -> None:
        if node_id in node_ids:
            return
        node_ids.add(node_id)
        entities.append(
            ProposedEntity(
                id=node_id,
                name=node_id,
                type="event",
                source_ref=SourceRef(file=source_file, locator=f"ink:{node_id}@L{line_no}"),
            )
        )

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if in_block_comment:
            if "*/" in raw:
                in_block_comment = False
                raw = raw.split("*/", 1)[1]
            else:
                continue
        if "/*" in raw:
            in_block_comment = "*/" not in raw
            raw = raw.split("/*", 1)[0]
        line = _strip_comment(raw)
        if not line.strip():
            continue

        var = _VAR.match(line)
        if var:
            variables.append({"kind": var.group(1), "name": var.group(2), "value": var.group(3)})
            continue

        knot = _KNOT.match(line)
        if knot:
            if knot.group(1):  # `=== function foo ===` — not a narrative node
                continue
            current_knot = knot.group(2)
            container = current_knot
            add_node(container, line_no)
            continue
        stitch = _STITCH.match(line)
        if stitch and current_knot:
            container = f"{current_knot}.{stitch.group(1)}"
            add_node(container, line_no)
            continue

        for target in _DIVERT.findall(line):
            if target in _SPECIAL_TARGETS:
                continue
            raw_diverts.append((container or "(root)", target, line_no))

    relations: list[ProposedRelation] = []
    for src, target, line_no in raw_diverts:
        if src not in node_ids:
            continue  # a divert before any knot (root weave) — no node to attach it to
        if target in node_ids:
            endpoint: str | None = target
        else:  # a bare stitch name resolves within the current knot
            qualified = f"{current_knot_of(src)}.{target}"
            endpoint = qualified if qualified in node_ids else None
        if endpoint is None:
            warnings.append(f"L{line_no}: divert `-> {target}` 指向未识别节点，保留待人审")
            continue
        relations.append(
            ProposedRelation(
                source=src,
                target=endpoint,
                kind="leads_to",
                evidence=f"-> {target}",
                source_ref=SourceRef(file=source_file, locator=f"ink:{src}->{target}@L{line_no}"),
            )
        )

    return ImportPlan(
        source_format="ink",
        entities=entities,
        relations=relations,
        variables=variables,
        warnings=warnings,
    )


def current_knot_of(container: str) -> str:
    """The knot a container belongs to (``knot`` or ``knot.stitch`` -> ``knot``)."""
    return container.split(".", 1)[0]
