"""The Intercept vendoring + the ink importer's per-construct losslessness ledger.

These tests exercise the production code path (``recognize/ink.py`` via
``recognize/ink_losslessness.py``) over the real, MIT-licensed inkle *The Intercept* fixture. The
commit is pinned, so the ground-truth construct counts are stable and safe to assert exactly.
"""

from __future__ import annotations

import hashlib

from owcopilot.recognize.ink import recognize_ink, translate_ink_condition
from owcopilot.recognize.ink_losslessness import (
    THE_INTERCEPT_FIXTURE,
    THE_INTERCEPT_SHA,
    THE_INTERCEPT_SHA256,
    build_ledger,
    render_markdown,
)

_FIXTURE_DIR = THE_INTERCEPT_FIXTURE.parent


def _ledger():
    text = THE_INTERCEPT_FIXTURE.read_text(encoding="utf-8")
    return build_ledger(text, source_file=THE_INTERCEPT_FIXTURE.name)


# --- vendoring / provenance --------------------------------------------------------------------
def test_the_intercept_fixture_is_vendored_with_matching_checksum() -> None:
    raw = THE_INTERCEPT_FIXTURE.read_bytes()
    assert len(raw) > 50_000  # the real ~1700-line script, not a stub
    assert hashlib.sha256(raw).hexdigest() == THE_INTERCEPT_SHA256  # exact upstream bytes


def test_the_intercept_license_and_source_are_recorded() -> None:
    license_text = (_FIXTURE_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2016 inkle Ltd." in license_text  # verbatim upstream copyright line

    source = (_FIXTURE_DIR / "SOURCE.md").read_text(encoding="utf-8")
    assert THE_INTERCEPT_SHA in source  # the pinned commit is documented, not just constant-coded
    assert "inkle/the-intercept" in source
    assert THE_INTERCEPT_SHA256 in source


# --- the ledger: nothing is dropped in silence -------------------------------------------------
def test_ledger_balances_every_construct_into_a_bucket() -> None:
    ledger = _ledger()
    # Each row's occurrences equal the sum of its disposition buckets — no occurrence is unbucketed.
    for row in ledger.rows:
        assert row.occurrences == row.captured + row.skipped + row.deferred + row.flagged
    assert ledger.silently_dropped == 0


def test_ledger_ground_truth_counts_are_stable_at_the_pinned_commit() -> None:
    ledger = _ledger()
    by_name = {r.construct: r for r in ledger.rows}

    # Structure is captured in full.
    assert by_name["Knot (=== knot)"].captured == 30
    assert by_name["Stitch (= stitch)"].captured == 30
    assert by_name["Declaration (VAR/CONST/LIST)"].captured == 27
    assert by_name["Gather — labelled (- (name))"].captured == 12

    # ink logic / out-of-scope constructs are counted and skipped, not mis-captured.
    assert by_name["Function knot (=== function)"].skipped == 2
    assert by_name["Tunnel (-> x -> / ->->)"].skipped == 5

    # Choices and their guards are the new coverage.
    choices = by_name["Choice (* / +)"]
    assert choices.occurrences == 338
    assert choices.captured == 40  # choices with a resolvable same-line divert → guarded edges

    guards = by_name["Conditional choice guard ({cond})"]
    assert guards.occurrences == 61
    assert guards.captured == 59  # translatable into the logic DSL
    assert guards.flagged == 2  # ink `&&` guards — kept as raw text in a warning, never coerced

    assert ledger.total_occurrences == 894
    assert ledger.silently_dropped == 0


def test_the_intercept_untranslatable_guards_are_surfaced_as_warnings() -> None:
    """The report claims every guard the DSL cannot express is kept verbatim in a warning. Both of
    The Intercept's untranslatable guards sit on choices that fall through into the weave (no
    same-line divert), so this pins that they are actually present in ImportPlan.warnings — the
    claim is provable, not aspirational."""
    plan = recognize_ink(
        THE_INTERCEPT_FIXTURE.read_text(encoding="utf-8"),
        source_file=THE_INTERCEPT_FIXTURE.name,
    )
    untranslatable = [w for w in plan.warnings if "DSL" in w and "选项条件" in w]

    def surfaced(*needles: str) -> bool:
        # Whitespace-robust: the raw guard body is preserved verbatim (internal spacing kept as-is),
        # so match on distinctive identifiers plus the `&&` operator the DSL cannot express.
        return any(all(n in w for n in needles) for w in untranslatable)

    assert surfaced("try_the_door", "try_the_windows", "&&")
    assert surfaced("gotcomponent", "throwncomponentaway", "&&")
    assert len(untranslatable) == 2  # exactly the two `&&` guards, matching guards.flagged == 2


def test_render_markdown_reports_zero_silent_drops() -> None:
    report = render_markdown(_ledger(), corpus="TheIntercept.ink")
    assert "silently dropped: **0**" in report
    assert THE_INTERCEPT_SHA[:12] in report
    assert "| Choice (* / +) |" in report


# --- condition translation: logic.expr is the sole oracle --------------------------------------
def test_translate_ink_condition_uses_the_dsl_as_the_oracle() -> None:
    assert translate_ink_condition(["teacup"]) == ("teacup", True)
    assert translate_ink_condition(["not drugged"]) == ("not drugged", True)
    assert translate_ink_condition(["forceful > 0"]) == ("forceful > 0", True)
    assert translate_ink_condition(["hooperClueType == NONE"]) == ("hooperClueType == NONE", True)
    # Stacked guards combine as logical AND (ink semantics).
    assert translate_ink_condition(["a", "b"]) == ("(a) and (b)", True)
    # No guard = unconditional.
    assert translate_ink_condition([]) == ("", True)


def test_translate_ink_condition_flags_expressions_beyond_the_dsl() -> None:
    # ink's `&&`, function calls and arithmetic are not in the boolean DSL — reported, not faked.
    assert translate_ink_condition(["a && b"]) == ("", False)
    assert translate_ink_condition(["fn() > 2"]) == ("", False)
    assert translate_ink_condition(["x + 1 > 2"]) == ("", False)
