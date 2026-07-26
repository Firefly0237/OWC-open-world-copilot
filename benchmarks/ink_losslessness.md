# ink importer — per-construct losslessness ledger

- Corpus: **TheIntercept.ink** (inkle *The Intercept*, MIT). Upstream `2a816b56e61c`.
- Deterministic, `$0` (pure parse of the vendored fixture — no model, no network).
- Reproduce: `python scripts/run_ink_losslessness.py`
- **The point is not that we import everything — it is that nothing is dropped in silence.** Every construct occurrence lands in exactly one bucket: `captured` (materialized), `skipped` (out-of-scope, counted), `deferred` (a later task owns it), or `flagged` (kept for human review).

Construct occurrences accounted: **894** · silently dropped: **0** · materialized into the ImportPlan: **299** · warnings surfaced for review: **45**.

| construct | occurrences | captured | rest (by bucket) | note |
|---|--:|--:|---|---|
| Knot (=== knot) | 30 | 30 | — | each becomes an `event` node |
| Function knot (=== function) | 2 | 0 | 2 skipped | ink logic, not a narrative node |
| Stitch (= stitch) | 30 | 30 | — | sub-node `knot.stitch` |
| Gather — labelled (- (name)) | 12 | 12 | — | addressable node `knot.name` (12 labels → 11 ids, 1 name reused) |
| Gather — anonymous (-) | 83 | 0 | 83 skipped | weave join point, no id (its flow is captured via its diverts) |
| Choice (* / +) | 338 | 40 | 271 deferred, 27 flagged | 40 become a `leads_to` edge (evidence = the shown text); 271 continue into the weave with no explicit target; 27 target an unresolved name |
| Conditional choice guard ({cond}) | 61 | 59 | 2 flagged | translated into the logic DSL (logic/expr.py is the sole oracle); 2 beyond the DSL, raw text kept in a warning |
| Divert — content line (->) | 121 | 101 | 4 skipped, 16 flagged | 101 resolve to an edge; 16 target an unresolved name; 4 sit in root weave (no source node) |
| Logic assignment (~) | 84 | 0 | 84 deferred | sets a variable/effect — the model now carries DialogueChoice.effects, but this importer emits a flow graph (nodes/edges), not DialogueTree objects, so there is still no place to hang them; deferred until the importer materializes trees |
| Tunnel (-> x -> / ->->) | 5 | 0 | 5 skipped | out-of-scope call/return — counted, never mis-captured as a plain edge |
| Thread (<-) | 0 | 0 | — | out-of-scope weave thread |
| Glue (<>) | 37 | 0 | 37 skipped | presentation only, no structure |
| Tag (#) | 0 | 0 | — | metadata annotation, no flow |
| Inline conditional / sequence text | 64 | 0 | 64 skipped | `{cond: a|b}` / `{|a|b|}` output — opaque prose, not parsed |
| Declaration (VAR/CONST/LIST) | 27 | 27 | — | lifted into ImportPlan.variables |

## Reading it

- **Structure is captured in full.** Every knot, stitch, labelled gather and declaration becomes a node or a variable (299 materialized items) — zero structural loss on the flow skeleton.
- **Choices and their guards are the new coverage.** A choice with a statement-level `-> target` becomes a `leads_to` edge carrying the choice's shown text as evidence; its `{condition}` guard is translated into the logic DSL. `logic/expr.py` is the sole oracle — a guard it cannot express (e.g. ink's `&&`) is kept verbatim in a warning, never silently coerced.
- **Out-of-scope constructs are counted, not mis-captured.** Tunnels used to be lifted as bogus plain edges; they are now recognized and skipped. Glue, tags, threads and inline conditional/sequence text carry no flow structure and are tallied here for honesty.
- **Deferred is not lost.** `~` logic lines set variables/effects and choices that fall through the weave carry no explicit target. The content model now has the receiving slot (`DialogueChoice.effects`, plus walk/explore semantics in `logic/dialogue.py`), but this importer emits a flow graph rather than DialogueTree objects, so materialization stays deferred until the importer grows tree output. Counted so the deferral is visible.
- **`flagged` rows are the review surface.** An unresolved divert target — often an ink choice *label*, which ink addresses but this pass does not register as a node — is kept as a warning for a human, exactly as the importer has always done for dangling targets.
