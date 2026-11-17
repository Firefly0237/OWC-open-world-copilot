# 从 ink 脚本（inkle 叙事语言）识别叙事结构。
"""Recognize narrative structure from an ink script (inkle's scripting language).

ink is text-first, so the safe deterministic signal is the *flow skeleton*: knots/stitches (and
labelled gathers) as narrative nodes, ``->`` diverts and player choices as edges between them, and
declared variables. Character/faction inference from dialogue prose stays in the optional
§8-guarded LLM pass — never guessed here.

The whole file is read once by :func:`_scan`, the importer's single structural pass, so there is
never a second, drifting parser. ``_scan`` tracks block-comment and brace-nesting state so a leading
``-``/``*`` is only read as a weave gather/choice at statement level (brace depth 0) — inside a
``{…}`` conditional block those bullets are block branches, not weave markers (the file's opening
``{DEBUG:…}`` block is exactly this case). Braces in ink are always logic, never string content, so
tracking brace depth alone is enough — no string-literal state is needed.

Constructs we deliberately do NOT materialize are counted, never silently dropped: tunnels
(``-> x ->`` / ``->->``), threads (``<-``), glue (``<>``), tags (``#``), ``~`` logic assignments
(choice *effects* are N4), inline conditional/sequence text, and ink functions. An ink conditional
*choice guard* is translated into the logic DSL (``logic/expr.py``) — which is the sole oracle for
"expressible": if it parses we keep the DSL string, if it raises we keep the raw text as a warning.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..logic.expr import LogicSyntaxError, parse_expr
from .models import ImportPlan, ProposedEntity, ProposedRelation, SourceRef

# === knot ===  /  == knot  (any number of '='); capture first identifier, flag `function` knots.
_KNOT = re.compile(r"^\s*={2,}\s*(function\s+)?([^\W\d]\w*)")
# = stitch  (exactly one leading '=')
_STITCH = re.compile(r"^\s*=(?!=)\s*([^\W\d]\w*)")
# -> target  (divert); target is an identifier, possibly dotted (knot.stitch). Stops at params/glue.
_DIVERT = re.compile(r"->\s*([^\W\d][\w.]*)")
_VAR = re.compile(r"^\s*(VAR|CONST|LIST)\s+([^\W\d]\w*)\s*=\s*(.+?)\s*$")
_COMMENT = re.compile(r"//.*$")
# A choice bullet run (`*` once-only, `+` sticky); nesting depth = number of bullets.
_CHOICE = re.compile(r"^\s*([*+]+)\s*")
# A gather bullet run (`-`), but NOT the `->` of a divert.
_GATHER = re.compile(r"^\s*(-+)(?!>)\s*")
# A `(name)` label immediately after a bullet run — the addressable name of a choice/gather.
_LABEL = re.compile(r"^\(([^\W\d]\w*)\)\s*")
# A leading `{…}` group with no nested braces — a candidate conditional-choice guard.
_GUARD = re.compile(r"^\{([^{}]*)\}\s*")
# A `~` logic line (assignment / function call / return).
_ASSIGN = re.compile(r"^\s*~\s*(.+?)\s*$")
# A `<- knot` thread pull.
_THREAD = re.compile(r"^\s*<-\s*[^\W\d][\w.]*")
# A tunnel: `-> target ->` (call) or `->->` (return). Never emitted as a plain edge.
_TUNNEL = re.compile(r"->\s*[^\W\d][\w.]*\s*->|->->")
# Choice display text is split by `[…]`: before-bracket prints in both, inside-bracket choice-only.
_BRACKET = re.compile(r"\[([^\]]*)\]")
_WHITESPACE = re.compile(r"\s+")

# Divert targets that are control flow, not nodes:
_SPECIAL_TARGETS = {"END", "DONE", "->", "ref"}


@dataclass(frozen=True)
class _Node:
    """A narrative node lifted from the script (a knot, a stitch, or a labelled gather)."""

    id: str
    kind: str  # "knot" | "stitch" | "gather_label"
    line: int


@dataclass(frozen=True)
class _Edge:
    """An unresolved flow edge: ``container -> target``, resolved against nodes in a second pass."""

    container: str
    target: str
    line: int
    via: str  # "content" | "choice"
    evidence: str
    guards: tuple[str, ...]  # raw ink guard bodies (choices only); stacked guards = logical AND


@dataclass(frozen=True)
class _Choice:
    """A player choice at statement level (for the ledger; its edge, if any, lands in ``edges``)."""

    container: str
    line: int
    marker: str  # "once" | "sticky"
    depth: int
    guards: tuple[str, ...]
    display_text: str
    has_target: bool


@dataclass(frozen=True)
class _Gather:
    container: str
    line: int
    depth: int
    label: str | None


@dataclass
class _Scan:
    """The result of the single structural pass — pure structure, no resolution or DSL yet."""

    nodes: list[_Node] = field(default_factory=list)
    edges: list[_Edge] = field(default_factory=list)
    choices: list[_Choice] = field(default_factory=list)
    gathers: list[_Gather] = field(default_factory=list)
    declarations: list[dict[str, object]] = field(default_factory=list)
    assignments: int = 0
    functions: int = 0
    tunnels: int = 0
    threads: int = 0
    glue: int = 0
    tags: int = 0
    inline_conditionals: int = 0

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


def _strip_comment(line: str) -> str:
    return _COMMENT.sub("", line)


def _looks_like_guard(body: str) -> bool:
    """A conditional-choice guard is a boolean ``{expr}``. A leading ``{…}`` that carries a
    top-level ``:`` (conditional text) or ``|`` (sequence/alternatives) is output, not a guard —
    leave it in the display text and count it as an inline conditional instead."""
    return ":" not in body and "|" not in body


def _peel_label(text: str) -> tuple[str | None, str]:
    match = _LABEL.match(text)
    if not match:
        return None, text
    return match.group(1), text[match.end() :]


def _peel_guards(text: str) -> tuple[list[str], str]:
    """Consume the stacked leading ``{…}`` boolean guards of a choice, returning (guards, rest). A
    leading ``{…}`` that is conditional text/sequence (``:`` or ``|`` inside) is not a guard: stop
    peeling and leave it in ``rest`` as display — the line-level scan tallies it as inline."""
    guards: list[str] = []
    while True:
        match = _GUARD.match(text)
        if not match:
            return guards, text
        body = match.group(1)
        if not _looks_like_guard(body):
            return guards, text
        guards.append(body.strip())
        text = text[match.end() :]


def _split_flow_target(text: str) -> tuple[str | None, str, bool]:
    """Split a choice/gather body into (target, remaining_text, is_tunnel). The flow target is the
    trailing ``-> id`` divert; a tunnel (``-> x ->`` / ``->->``) yields no plain target."""
    if _TUNNEL.search(text):
        return None, text, True
    matches = list(_DIVERT.finditer(text))
    if not matches:
        return None, text, False
    last = matches[-1]
    return last.group(1), text[: last.start()], False


def _display_text(text: str) -> str:
    """What the choice prints in the menu: before-bracket text plus the inside-bracket text."""
    match = _BRACKET.search(text)
    if match:
        shown = text[: match.start()] + match.group(1)
    else:
        shown = text
    return _WHITESPACE.sub(" ", shown).strip()


def _scan(text: str) -> _Scan:
    """Single structural pass over an ink script. Statement-level (brace depth 0) lines are
    classified once, in this order: thread → choice → gather → var → knot → stitch → assignment →
    residual content-diverts. Inside a ``{…}`` block (brace depth > 0) bullets are block branches,
    so only plain content diverts are lifted there (exactly as the pre-refactor importer did)."""
    scan = _Scan()
    current_knot: str | None = None
    container: str | None = None
    in_block_comment = False
    brace_depth = 0

    def add_node(node_id: str, kind: str, line_no: int) -> None:
        if node_id not in scan.node_ids():
            scan.nodes.append(_Node(id=node_id, kind=kind, line=line_no))

    def emit_content_diverts(line: str, line_no: int) -> None:
        """Lift plain ``-> target`` diverts from a line — the pre-refactor behaviour, minus
        tunnels (an out-of-scope construct, counted and skipped, not mis-captured as an edge)."""
        if _TUNNEL.search(line):
            scan.tunnels += len(_TUNNEL.findall(line))
            return
        for target in _DIVERT.findall(line):
            if target in _SPECIAL_TARGETS:
                continue
            scan.edges.append(
                _Edge(
                    container=container or "(root)",
                    target=target,
                    line=line_no,
                    via="content",
                    evidence=f"-> {target}",
                    guards=(),
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
        # Cheap skip-construct tallies (glue/tags are formatting; count occurrences, drop nothing).
        scan.glue += line.count("<>")
        scan.tags += len(re.findall(r"(?<!\\)#", line))
        scan.inline_conditionals += len(re.findall(r"\{[^{}]*[:|][^{}]*\}", line))

        line_start_depth = brace_depth
        brace_depth += line.count("{") - line.count("}")

        if not line.strip():
            continue

        if line_start_depth > 0:
            # Inside a conditional block: bullets are block branches, not weave markers. Only plain
            # content diverts are lifted, so real targets in an embedded branch are not lost.
            emit_content_diverts(line, line_no)
            continue

        if _THREAD.match(line):
            scan.threads += 1
            continue

        choice = _CHOICE.match(line)
        if choice:
            bullets = choice.group(1)
            rest = line[choice.end() :]
            _label, rest = _peel_label(rest)
            guards, rest = _peel_guards(rest)
            target, body, is_tunnel = _split_flow_target(rest)
            if is_tunnel:
                scan.tunnels += len(_TUNNEL.findall(rest))
            display = _display_text(body)
            has_target = bool(target) and target not in _SPECIAL_TARGETS
            scan.choices.append(
                _Choice(
                    container=container or "(root)",
                    line=line_no,
                    marker="sticky" if "+" in bullets else "once",
                    depth=len(bullets),
                    guards=tuple(guards),
                    display_text=display,
                    has_target=has_target,
                )
            )
            if has_target and target is not None:
                scan.edges.append(
                    _Edge(
                        container=container or "(root)",
                        target=target,
                        line=line_no,
                        via="choice",
                        evidence=display or f"-> {target}",
                        guards=tuple(guards),
                    )
                )
            continue

        gather = _GATHER.match(line)
        if gather:
            rest = line[gather.end() :]
            label, rest = _peel_label(rest)
            if label and current_knot:
                add_node(f"{current_knot}.{label}", "gather_label", line_no)
            scan.gathers.append(
                _Gather(
                    container=container or "(root)",
                    line=line_no,
                    depth=len(gather.group(1)),
                    label=label,
                )
            )
            # A gather may carry a same-line divert (its continuation) — lift it like content.
            emit_content_diverts(line, line_no)
            continue

        var = _VAR.match(line)
        if var:
            scan.declarations.append(
                {"kind": var.group(1), "name": var.group(2), "value": var.group(3)}
            )
            continue

        knot = _KNOT.match(line)
        if knot:
            if knot.group(1):  # `=== function foo ===` — logic, not a narrative node.
                scan.functions += 1
                continue
            current_knot = knot.group(2)
            container = current_knot
            add_node(container, "knot", line_no)
            continue

        stitch = _STITCH.match(line)
        if stitch and current_knot:
            container = f"{current_knot}.{stitch.group(1)}"
            add_node(container, "stitch", line_no)
            continue

        if _ASSIGN.match(line):
            scan.assignments += 1  # choice/stage effects are N4; counted here, not materialized.
            continue

        emit_content_diverts(line, line_no)

    return scan


def translate_ink_condition(guards: Sequence[str]) -> tuple[str, bool]:
    """Translate ink choice guard(s) into the logic DSL. Stacked guards combine as logical AND
    (ink semantics). ``logic.expr.parse_expr`` is the SOLE oracle for translatability — we never
    write a second boolean parser and never ``eval``. Returns ``(dsl, translatable)``; an
    untranslatable guard yields ``("", False)`` so the caller can preserve the raw text instead."""
    parts = [g.strip() for g in guards if g.strip()]
    if not parts:
        return "", True
    dsl = " and ".join(f"({p})" for p in parts) if len(parts) > 1 else parts[0]
    try:
        parse_expr(dsl)
    except LogicSyntaxError:
        return "", False
    return dsl, True


@dataclass(frozen=True)
class InkAnalysis:
    """Everything both the importer and the losslessness ledger need, computed exactly once so the
    ledger can never disagree with what :func:`recognize_ink` actually produced."""

    scan: _Scan
    entities: list[ProposedEntity]
    relations: list[ProposedRelation]
    variables: list[dict[str, object]]
    warnings: list[str]
    content_edges_resolved: int
    content_edges_unresolved: int
    content_edges_no_source: int
    choice_edges_resolved: int
    choice_edges_unresolved: int
    choice_edges_no_source: int
    conditions_translated: int
    conditions_untranslatable: int


def _current_knot_of(container: str) -> str:
    """The knot a container belongs to (``knot`` or ``knot.stitch``/``knot.label`` -> ``knot``)."""
    return container.split(".", 1)[0]


def analyze_ink(text: str, *, source_file: str = "") -> InkAnalysis:
    """Scan the script, then resolve edges against the discovered nodes and translate choice guards.
    Content-divert resolution is byte-for-byte the pre-refactor behaviour; choice edges additionally
    carry the choice's display text as evidence and its translated guard as ``condition``."""
    scan = _scan(text)
    node_ids = scan.node_ids()

    entities = [
        ProposedEntity(
            id=node.id,
            name=node.id,
            type="event",
            source_ref=SourceRef(file=source_file, locator=f"ink:{node.id}@L{node.line}"),
        )
        for node in scan.nodes
    ]

    relations: list[ProposedRelation] = []
    warnings: list[str] = []
    content_ok = content_bad = content_nosrc = 0
    choice_ok = choice_bad = choice_nosrc = 0
    cond_ok = cond_bad = 0
    # Lines of choices whose guard was already surfaced as an untranslatable warning via their edge,
    # so the post-loop sweep over guard-only choices never double-reports the same guard.
    guard_warned_lines: set[int] = set()

    for edge in scan.edges:
        if edge.container not in node_ids:
            # A divert before any knot (root weave) — no node to attach it to. Counted, not lost.
            if edge.via == "choice":
                choice_nosrc += 1
            else:
                content_nosrc += 1
            continue
        if edge.target in node_ids:
            endpoint: str | None = edge.target
        else:  # a bare stitch/label name resolves within the current knot
            qualified = f"{_current_knot_of(edge.container)}.{edge.target}"
            endpoint = qualified if qualified in node_ids else None

        if endpoint is None:
            warnings.append(f"L{edge.line}: divert `-> {edge.target}` 指向未识别节点，保留待人审")
            if edge.via == "choice":
                choice_bad += 1
            else:
                content_bad += 1
            continue

        condition = ""
        if edge.guards:
            condition, ok = translate_ink_condition(edge.guards)
            if ok:
                cond_ok += 1
            else:
                cond_bad += 1
                guard_warned_lines.add(edge.line)
                raw = " ".join(f"{{{g}}}" for g in edge.guards)
                warnings.append(
                    f"L{edge.line}: 选项条件 `{raw}` 无法翻译为逻辑 DSL，保留原文待人审"
                )

        relations.append(
            ProposedRelation(
                source=edge.container,
                target=endpoint,
                kind="leads_to",
                evidence=edge.evidence,
                condition=condition,
                source_ref=SourceRef(
                    file=source_file, locator=f"ink:{edge.container}->{edge.target}@L{edge.line}"
                ),
            )
        )
        if edge.via == "choice":
            choice_ok += 1
        else:
            content_ok += 1

    # A guarded choice that continues into the weave carries no same-line divert, so it never
    # entered the edge loop above. Its guard is still real content: if the DSL cannot express it,
    # surface the raw text as a warning here too — otherwise an untranslatable guard on such a
    # choice (e.g. The Intercept's `{a && b}` gates) would be silently lost from the review surface.
    for choice in scan.choices:
        if not choice.guards or choice.line in guard_warned_lines:
            continue
        _, ok = translate_ink_condition(choice.guards)
        if ok:
            continue
        cond_bad += 1
        raw = " ".join(f"{{{g}}}" for g in choice.guards)
        warnings.append(f"L{choice.line}: 选项条件 `{raw}` 无法翻译为逻辑 DSL，保留原文待人审")

    return InkAnalysis(
        scan=scan,
        entities=entities,
        relations=relations,
        variables=list(scan.declarations),
        warnings=warnings,
        content_edges_resolved=content_ok,
        content_edges_unresolved=content_bad,
        content_edges_no_source=content_nosrc,
        choice_edges_resolved=choice_ok,
        choice_edges_unresolved=choice_bad,
        choice_edges_no_source=choice_nosrc,
        conditions_translated=cond_ok,
        conditions_untranslatable=cond_bad,
    )


def recognize_ink(text: str, *, source_file: str = "") -> ImportPlan:
    """Extract knots/stitches/labelled-gathers as nodes, diverts and choices as ``leads_to`` edges
    (choices carry their display text and translated guard), and declared variables."""
    analysis = analyze_ink(text, source_file=source_file)
    return ImportPlan(
        source_format="ink",
        entities=analysis.entities,
        relations=analysis.relations,
        variables=analysis.variables,
        warnings=analysis.warnings,
    )
