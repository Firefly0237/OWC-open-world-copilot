"""Stable line identity and actual source text/export contracts, no model simulation."""

import csv
from copy import deepcopy
from io import StringIO

import pytest

from owcopilot.content.models import ContentBundle
from owcopilot.exporters.xliff import write_localization_csv
from owcopilot.workflow.mechanic_ir_v2 import (
    compile_mechanic_design,
    compile_mechanic_dialogue,
    materialize_quest_draft,
    seal_compilation_quest,
)
from owcopilot.workflow.narrative_localization_v2 import source_localization_entries
from owcopilot.workflow.narrative_recompile_v2 import recompile_bound_localization
from tests.domain.test_mechanic_ir_v2 import design_case


def test_keys_stay_stable_when_source_text_changes_and_fake_table_is_rejected():
    raw = {"title": "First", "objective": "Original", "stages": [{"id": "s", "summary": "A line"}]}
    first = materialize_quest_draft(raw, quest_id="q_same")
    second = materialize_quest_draft({**raw, "objective": "A revised line"}, quest_id="q_same")
    assert first.localization_keys == second.localization_keys
    assert {row.text_key: row.text for row in source_localization_entries(second)}[
        "quest.q_same.objective"
    ] == "A revised line"
    bad = second.model_copy(deep=True)
    bad.metadata["localization_v2"]["source_strings"]["quest.q_same.objective"] = "Fake source"
    with pytest.raises(ValueError, match="原文表"):
        source_localization_entries(bad)


def test_recompilation_preserves_real_mechanics_routes_and_exports_actual_strings(tmp_path):
    plan, quest, ir, prose = design_case()
    compiled = compile_mechanic_design(ir, plan, quest, require_distinct=True)
    dialogue = compile_mechanic_dialogue(prose, compiled, plan, tree_id="tree_dev")
    source = {
        "quest": seal_compilation_quest(compiled).model_dump(mode="json"),
        "tree": dialogue.tree.model_dump(mode="json"),
    }
    original = deepcopy(source)
    receipt = recompile_bound_localization(plan, source)
    assert source == original and receipt["invariants"]["quest_logic_unchanged"]
    bundle = ContentBundle(
        quests={compiled.quest.id: compiled.quest}, dialogue_trees={dialogue.tree.id: dialogue.tree}
    )
    for entry in source_localization_entries(compiled.quest):
        bundle.localized_texts[entry.id] = entry
    output = tmp_path / "strings.csv"
    write_localization_csv(bundle, output)
    rows = list(csv.DictReader(StringIO(output.read_text(encoding="utf-8-sig"))))
    assert len(rows) == len(receipt["localization_entries"])
    source["tree"]["nodes"][dialogue.tree.root_node]["text"] = "Changed without source IR"
    with pytest.raises(ValueError, match="对白原文"):
        recompile_bound_localization(plan, source)
