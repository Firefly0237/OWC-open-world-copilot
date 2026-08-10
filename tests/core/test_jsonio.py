from __future__ import annotations

import pytest

from owcopilot.llm.jsonio import extract_json, extract_json_object


def test_extract_object_tolerates_prose_and_fences() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('Here is the world:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}
    assert extract_json_object('Sure! {"a": 1, "b": [2, 3]} hope that helps') == {
        "a": 1,
        "b": [2, 3],
    }


def test_extract_object_raises_on_no_object_or_invalid() -> None:
    with pytest.raises(ValueError):
        extract_json_object("no json here at all")
    with pytest.raises(ValueError):
        extract_json_object('{"a": 1,')  # truncated / invalid
    with pytest.raises(ValueError):
        extract_json_object("[1, 2, 3]")  # an array is not an object


def test_extract_json_handles_object_or_array() -> None:
    assert extract_json('["a", "b"]') == ["a", "b"]
    assert extract_json('{"variants": ["x"]}') == {"variants": ["x"]}
    assert extract_json("prose then [1, 2] tail") == [1, 2]
    with pytest.raises(ValueError):
        extract_json("nothing parseable")
