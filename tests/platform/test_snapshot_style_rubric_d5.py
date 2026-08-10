# D5 修复回归测试：style_rubric（第 12 canon 类）必须接入 snapshot.py 的版本 diff 与三方合并。
"""D5 critical-data-loss fix: ``style_rubric`` must be wired into snapshot.py's diff/merge.

``StyleRubric`` (content/models.py, the 12th canon type) was fully wired through the store's
load/save/COW-overlay/tombstone path, but ``content/snapshot.py`` had two separate registries
that both silently omitted it:

* ``_COLLECTIONS`` -- so a version diff could never show a rubric being added/removed/edited.
* ``_MERGE_DICT_KINDS`` -- so ``merge_bundles_three_way`` (the core of ``merge_versions``, which
  the update-ingest three-way merge path calls) silently DROPPED every ``style_rubrics`` entry
  from its output, on every merge, for every world that had one. Real data loss.

These tests exercise both fixes directly against the same collections/merge implementation
production code paths use (no reimplementation of the merge/diff logic)."""

from __future__ import annotations

from owcopilot.content.models import ContentBundle, StyleRubric, StyleRubricElement
from owcopilot.content.snapshot import bundle_diff, merge_bundles_three_way, merge_versions
from owcopilot.content.store import ContentStore


def _rubric(rid: str, label: str, *element_texts: str) -> StyleRubric:
    return StyleRubric(
        id=rid,
        label=label,
        abstract_tag="日式风格",
        elements=[
            StyleRubricElement(id=f"{rid}_{i}", text=text) for i, text in enumerate(element_texts)
        ],
    )


# --- A1: bundle_diff / version_diff must see rubric changes -----------------


def test_bundle_diff_reports_added_removed_and_changed_style_rubric() -> None:
    old = ContentBundle(style_rubrics={"r1": _rubric("r1", "和风浓度 v1", "应出现鸟居")})
    new = ContentBundle(
        style_rubrics={"r1": _rubric("r1", "和风浓度 v2", "应出现鸟居", "不应出现十字架")}
    )
    diff = bundle_diff(old, new)
    changed = {(c.kind, c.id): c for c in diff.changed}
    assert ("style_rubric", "r1") in changed
    fields = {fc.field for fc in changed[("style_rubric", "r1")].changes}
    assert "label" in fields and "elements" in fields
    assert changed[("style_rubric", "r1")].name == "和风浓度 v2"  # name_field="label" resolved

    added = bundle_diff(ContentBundle(), new)
    assert ("style_rubric", "r1") in {(c.kind, c.id) for c in added.added}
    removed = bundle_diff(old, ContentBundle())
    assert ("style_rubric", "r1") in {(c.kind, c.id) for c in removed.removed}


def test_version_diff_via_store_sees_style_rubric_edit(tmp_path) -> None:
    store = ContentStore(tmp_path)
    store.save(ContentBundle(style_rubrics={"r1": _rubric("r1", "v1", "term_a")}))
    store.create_version("v2", base_version="v1")
    v2 = store.load_scoped(version="v2")
    v2.style_rubrics["r1"] = _rubric("r1", "v2", "term_a", "term_b")
    store.save_scoped(v2, version="v2")

    from owcopilot.content.snapshot import version_diff

    diff = version_diff(store, from_version="v1", to_version="v2")
    changed_kinds = {(c.kind, c.id) for c in diff.changed}
    assert ("style_rubric", "r1") in changed_kinds


# --- A1: merge_bundles_three_way must not drop style_rubrics -----------------


def test_merge_keeps_one_sided_rubric_changes_and_untouched_rubric() -> None:
    base = ContentBundle(
        style_rubrics={
            "r1": _rubric("r1", "base-r1", "term_a"),
            "r2": _rubric("r2", "base-r2", "term_x"),
        }
    )
    ours = ContentBundle(
        style_rubrics={
            "r1": _rubric("r1", "ours-r1", "term_a", "term_b"),  # only ours changes r1
            "r2": _rubric("r2", "base-r2", "term_x"),
        }
    )
    theirs = ContentBundle(
        style_rubrics={
            "r1": _rubric("r1", "base-r1", "term_a"),
            "r2": _rubric("r2", "theirs-r2", "term_x", "term_y"),  # only theirs changes r2
        }
    )
    result = merge_bundles_three_way(base=base, ours=ours, theirs=theirs)
    assert not result.conflicts
    # this is the exact bug: before the fix, merged.style_rubrics was always {} (kind absent
    # from _MERGE_DICT_KINDS meant the loop never touched the attribute at all).
    assert result.merged.style_rubrics["r1"].label == "ours-r1"
    assert result.merged.style_rubrics["r2"].label == "theirs-r2"


def test_merge_flags_both_changed_rubric_as_conflict_and_keeps_ours_placeholder() -> None:
    base = ContentBundle(style_rubrics={"r1": _rubric("r1", "base", "term_a")})
    ours = ContentBundle(style_rubrics={"r1": _rubric("r1", "ours", "term_a", "b")})
    theirs = ContentBundle(style_rubrics={"r1": _rubric("r1", "theirs", "term_a", "c")})
    result = merge_bundles_three_way(base=base, ours=ours, theirs=theirs)
    assert [(c.kind, c.id, c.reason) for c in result.conflicts] == [
        ("style_rubric", "r1", "both-changed")
    ]
    assert result.merged.style_rubrics["r1"].label == "ours"  # placeholder keeps ours


def test_merge_versions_end_to_end_preserves_rubrics_through_the_store(tmp_path) -> None:
    # same shape as test_merge_c4c.py's entity coverage, but for style_rubric -- proves the
    # store-backed merge_versions() path (what update-ingest's three-way merge calls), not just
    # the pure bundle_diff helper above.
    store = ContentStore(tmp_path)
    store.save(ContentBundle(style_rubrics={"r1": _rubric("r1", "base", "term_a")}))
    store.create_version("v2", base_version="v1")
    v2 = store.load_scoped(version="v2")
    v2.style_rubrics["r1"] = _rubric("r1", "v2-edit", "term_a", "term_b")
    store.save_scoped(v2, version="v2")

    store.create_version("v3", base_version="v1")
    v3 = store.load_scoped(version="v3")
    v3.style_rubrics["r_new"] = _rubric("r_new", "added-in-v3", "term_z")
    store.save_scoped(v3, version="v3")

    result = merge_versions(store, ours="v2", theirs="v3")
    assert not result.conflicts
    assert result.merged.style_rubrics["r1"].label == "v2-edit"
    assert result.merged.style_rubrics["r_new"].label == "added-in-v3"
