"""Quest schema robustness (P1 real-model hardening, audit C7).

Real models — even in JSON mode — sometimes return `prerequisites` as prose / null and `reward`
as a number. These coercions keep structured generation robust without a brittle re-prompt, so
they get direct unit coverage here (they were previously only exercised indirectly via the loop).
"""

import pytest

from owcopilot.generation.quest import Quest, parse_quest


def _q(**over):
    base = {"title": "T", "giver_npc": "Aldric", "location": "Northwatch", "objective": "o"}
    base.update(over)
    return Quest(**base)


# --------------------------------------------------------------- prerequisites coercion
def test_prerequisites_none_becomes_empty_list():
    assert _q(prerequisites=None).prerequisites == []


@pytest.mark.parametrize("sentinel", ["", "none", "N/A", "na", "no prerequisites", "-", "null"])
def test_prerequisites_none_like_strings_become_empty(sentinel):
    assert _q(prerequisites=sentinel).prerequisites == []


def test_prerequisites_prose_string_is_split_and_cleaned():
    # a chatty model returns a single string with separators + bullet/whitespace noise
    q = _q(prerequisites="The Caravan Ambush;\n - The Healer's Plea")
    assert q.prerequisites == ["The Caravan Ambush", "The Healer's Plea"]


def test_prerequisites_real_list_passes_through():
    assert _q(prerequisites=["A", "B"]).prerequisites == ["A", "B"]


# --------------------------------------------------------------- reward coercion
def test_numeric_reward_is_stringified():
    assert _q(reward=75).reward == "75"


def test_none_reward_becomes_empty_string():
    assert _q(reward=None).reward == ""


# --------------------------------------------------------------- timeline order coercion
def test_numeric_string_timeline_order_is_coerced():
    assert _q(timeline_order="3").timeline_order == 3


def test_empty_timeline_order_becomes_none():
    assert _q(timeline_order="").timeline_order is None


# --------------------------------------------------------------- parse_quest tolerates fences
def test_parse_quest_strips_json_code_fence():
    raw = (
        '```json\n{"title": "T", "giver_npc": "Aldric", "location": "Northwatch", '
        '"objective": "o", "reward": 50, "prerequisites": "none", "timeline_order": "2"}\n```'
    )
    q = parse_quest(raw)
    assert q.reward == "50"  # numeric reward coerced
    assert q.prerequisites == []  # "none" sentinel coerced
    assert q.timeline_order == 2
    assert q.location == "Northwatch"
