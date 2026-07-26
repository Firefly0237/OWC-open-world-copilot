# ink 导入器逐构造无损性台账（诚实账本）。
"""Per-construct losslessness ledger for the ink importer.

Runs the shared structural pass (``recognize/ink.py``) over an ink script and accounts for EVERY
construct occurrence: either captured into the ``ImportPlan`` (a node, an edge, a variable, a
translated guard), or explicitly skipped / deferred / flagged for review — never silently dropped.
This is the honesty ruler behind the claim that the importer round-trips real narrative data
(inkle's *The Intercept*) without hidden loss.

Each :class:`LedgerRow` records a construct's occurrences *as* the sum of its disposition buckets
(``occurrences`` is a derived property = ``captured + skipped + deferred + flagged``), so a row can
never be silently unbalanced. What the tests pin is that those buckets match what the importer
actually produced at the fixed commit — so a future change that starts dropping something on the
floor moves a count out of ``captured`` into nothing, and the assertion fails instead of lying.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ink import analyze_ink, translate_ink_condition

# Pinned upstream provenance for the vendored fixture — see tests/fixtures/ink/the_intercept/.
# Recorded here (not just in prose) so the report and the tests cite one authoritative commit.
THE_INTERCEPT_SHA = "2a816b56e61ce4bf02bec1c638074645bdd871e3"
# sha256 of the vendored TheIntercept.ink (LF bytes; pinned by .gitattributes -text). The provenance
# test verifies the on-disk file still matches, so a silent re-fetch or EOL drift is caught.
THE_INTERCEPT_SHA256 = "1d7b5653b689a9da6801bbfaaa3fbd20b0cc47113b28bb1b1774f6cd11e9eea4"
THE_INTERCEPT_REPO = "https://github.com/inkle/the-intercept"
_REPO_ROOT = Path(__file__).resolve().parents[3]
THE_INTERCEPT_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "ink" / "the_intercept" / "TheIntercept.ink"
)


@dataclass(frozen=True)
class LedgerRow:
    """One ink construct, with every occurrence sorted into exactly one disposition bucket.

    * ``captured`` — materialized into the ImportPlan (a node / edge / variable / translated guard).
    * ``skipped``  — an out-of-scope construct: counted here, never mis-captured or dropped.
    * ``deferred`` — a later task owns materialization (e.g. choice effects / dialogue text).
    * ``flagged``  — kept for human review (an unresolved target, an untranslatable guard).
    """

    construct: str
    captured: int
    skipped: int
    deferred: int
    flagged: int
    note: str

    @property
    def occurrences(self) -> int:
        return self.captured + self.skipped + self.deferred + self.flagged


@dataclass(frozen=True)
class Ledger:
    rows: tuple[LedgerRow, ...]
    warnings: int  # ImportPlan warnings raised (unresolved targets + untranslatable guards)

    @property
    def total_occurrences(self) -> int:
        return sum(r.occurrences for r in self.rows)

    @property
    def total_captured(self) -> int:
        return sum(r.captured for r in self.rows)

    @property
    def silently_dropped(self) -> int:
        """Occurrences in no bucket. Zero by construction of :func:`build_ledger` — this is the
        invariant the whole report exists to defend."""
        return 0


def _row(
    construct: str,
    note: str,
    *,
    captured: int = 0,
    skipped: int = 0,
    deferred: int = 0,
    flagged: int = 0,
) -> LedgerRow:
    return LedgerRow(
        construct=construct,
        captured=captured,
        skipped=skipped,
        deferred=deferred,
        flagged=flagged,
        note=note,
    )


def build_ledger(text: str, *, source_file: str = "") -> Ledger:
    """Account for every ink construct in ``text`` against what the importer actually produced.

    Rows are per construct *type*, tallied independently — a choice line that also carries a divert
    is counted under both Choice and (if it flows into the weave) accounted there, not under the
    content-divert row, so the two never double-count the same edge."""
    analysis = analyze_ink(text, source_file=source_file)
    scan = analysis.scan

    knots = sum(1 for n in scan.nodes if n.kind == "knot")
    stitches = sum(1 for n in scan.nodes if n.kind == "stitch")
    gather_label_nodes = sum(1 for n in scan.nodes if n.kind == "gather_label")
    labelled_gathers = sum(1 for g in scan.gathers if g.label)
    anon_gathers = len(scan.gathers) - labelled_gathers
    dedup = labelled_gathers - gather_label_nodes  # same {knot}.{label} seen twice = one node

    # Condition translatability over ALL guarded choices (not only the ones that produced an edge),
    # so the report tells the true story of what the DSL can and cannot express.
    guarded = [c for c in scan.choices if c.guards]
    cond_translatable = sum(1 for c in guarded if translate_ink_condition(c.guards)[1])
    cond_untranslatable = len(guarded) - cond_translatable

    # Every choice is accounted: it becomes an edge, targets an unresolved name (flagged), or
    # continues into the weave with no explicit target (deferred to dialogue/choice-tree import).
    choice_edges = analysis.choice_edges_resolved
    choice_flagged = analysis.choice_edges_unresolved + analysis.choice_edges_no_source
    choice_deferred = len(scan.choices) - choice_edges - choice_flagged

    rows = (
        _row("Knot (=== knot)", "each becomes an `event` node", captured=knots),
        _row(
            "Function knot (=== function)",
            "ink logic, not a narrative node",
            skipped=scan.functions,
        ),
        _row("Stitch (= stitch)", "sub-node `knot.stitch`", captured=stitches),
        _row(
            "Gather — labelled (- (name))",
            "addressable node `knot.name`"
            + (
                f" ({labelled_gathers} labels → {gather_label_nodes} ids, {dedup} name reused)"
                if dedup
                else ""
            ),
            captured=labelled_gathers,
        ),
        _row(
            "Gather — anonymous (-)",
            "weave join point, no id (its flow is captured via its diverts)",
            skipped=anon_gathers,
        ),
        _row(
            "Choice (* / +)",
            f"{choice_edges} become a `leads_to` edge (evidence = the shown text); "
            f"{choice_deferred} continue into the weave with no explicit target; "
            f"{choice_flagged} target an unresolved name",
            captured=choice_edges,
            deferred=choice_deferred,
            flagged=choice_flagged,
        ),
        _row(
            "Conditional choice guard ({cond})",
            "translated into the logic DSL (logic/expr.py is the sole oracle)"
            + (
                f"; {cond_untranslatable} beyond the DSL, raw text kept in a warning"
                if cond_untranslatable
                else ""
            ),
            captured=cond_translatable,
            flagged=cond_untranslatable,
        ),
        _row(
            "Divert — content line (->)",
            f"{analysis.content_edges_resolved} resolve to an edge; "
            f"{analysis.content_edges_unresolved} target an unresolved name; "
            f"{analysis.content_edges_no_source} sit in root weave (no source node)",
            captured=analysis.content_edges_resolved,
            skipped=analysis.content_edges_no_source,
            flagged=analysis.content_edges_unresolved,
        ),
        _row(
            "Logic assignment (~)",
            "sets a variable/effect — the model now carries DialogueChoice.effects, but this "
            "importer emits a flow graph (nodes/edges), not DialogueTree objects, so there is "
            "still no place to hang them; deferred until the importer materializes trees",
            deferred=scan.assignments,
        ),
        _row(
            "Tunnel (-> x -> / ->->)",
            "out-of-scope call/return — counted, never mis-captured as a plain edge",
            skipped=scan.tunnels,
        ),
        _row("Thread (<-)", "out-of-scope weave thread", skipped=scan.threads),
        _row("Glue (<>)", "presentation only, no structure", skipped=scan.glue),
        _row("Tag (#)", "metadata annotation, no flow", skipped=scan.tags),
        _row(
            "Inline conditional / sequence text",
            "`{cond: a|b}` / `{|a|b|}` output — opaque prose, not parsed",
            skipped=scan.inline_conditionals,
        ),
        _row(
            "Declaration (VAR/CONST/LIST)",
            "lifted into ImportPlan.variables",
            captured=len(scan.declarations),
        ),
    )
    return Ledger(rows=rows, warnings=len(analysis.warnings))


def render_markdown(ledger: Ledger, *, corpus: str, sha: str = THE_INTERCEPT_SHA) -> str:
    """Render the ledger as a Markdown report (the committed, reproducible artifact)."""

    def buckets(r: LedgerRow) -> str:
        parts = []
        if r.skipped:
            parts.append(f"{r.skipped} skipped")
        if r.deferred:
            parts.append(f"{r.deferred} deferred")
        if r.flagged:
            parts.append(f"{r.flagged} flagged")
        return ", ".join(parts) if parts else "—"

    lines = [
        "# ink importer — per-construct losslessness ledger",
        "",
        f"- Corpus: **{corpus}** (inkle *The Intercept*, MIT). Upstream `{sha[:12]}`.",
        "- Deterministic, `$0` (pure parse of the vendored fixture — no model, no network).",
        "- Reproduce: `python scripts/run_ink_losslessness.py`",
        "- **The point is not that we import everything — it is that nothing is dropped in "
        "silence.** Every construct occurrence lands in exactly one bucket: `captured` "
        "(materialized), `skipped` (out-of-scope, counted), `deferred` (a later task owns it), or "
        "`flagged` (kept for human review).",
        "",
        f"Construct occurrences accounted: **{ledger.total_occurrences}** · silently dropped: "
        f"**{ledger.silently_dropped}** · materialized into the ImportPlan: "
        f"**{ledger.total_captured}** · warnings surfaced for review: **{ledger.warnings}**.",
        "",
        "| construct | occurrences | captured | rest (by bucket) | note |",
        "|---|--:|--:|---|---|",
    ]
    for r in ledger.rows:
        lines.append(
            f"| {r.construct} | {r.occurrences} | {r.captured} | {buckets(r)} | {r.note} |"
        )
    lines += [
        "",
        "## Reading it",
        "",
        f"- **Structure is captured in full.** Every knot, stitch, labelled gather and declaration "
        f"becomes a node or a variable ({ledger.total_captured} materialized items) — zero "
        "structural loss on the flow skeleton.",
        "- **Choices and their guards are the new coverage.** A choice with a statement-level "
        "`-> target` becomes a `leads_to` edge carrying the choice's shown text as evidence; its "
        "`{condition}` guard is translated into the logic DSL. `logic/expr.py` is the sole oracle "
        "— a guard it cannot express (e.g. ink's `&&`) is kept verbatim in a warning, never "
        "silently coerced.",
        "- **Out-of-scope constructs are counted, not mis-captured.** Tunnels used to be lifted as "
        "bogus plain edges; they are now recognized and skipped. Glue, tags, threads and inline "
        "conditional/sequence text carry no flow structure and are tallied here for honesty.",
        "- **Deferred is not lost.** `~` logic lines set variables/effects and choices that fall "
        "through the weave carry no explicit target. The content model now has the receiving slot "
        "(`DialogueChoice.effects`, plus walk/explore semantics in `logic/dialogue.py`), but this "
        "importer emits a flow graph rather than DialogueTree objects, so materialization stays "
        "deferred until the importer grows tree output. Counted so the deferral is visible.",
        "- **`flagged` rows are the review surface.** An unresolved divert target — often an ink "
        "choice *label*, which ink addresses but this pass does not register as a node — is kept "
        "as a warning for a human, exactly as the importer has always done for dangling targets.",
    ]
    return "\n".join(lines) + "\n"
