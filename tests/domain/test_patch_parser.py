from __future__ import annotations

import pytest

from owcopilot.patches.parser import parse_patch_candidates


def test_parse_patch_candidates_accepts_candidates_wrapper() -> None:
    candidates = parse_patch_candidates(
        """
{
  "candidates": [
    {
      "rationale": "Fix desc",
      "ops": [
        {"op": "replace", "path": "/entities/npc_aldric/description", "value": "New"}
      ]
    }
  ]
}
""".strip()
    )

    assert len(candidates) == 1
    assert candidates[0].ops[0].path == "/entities/npc_aldric/description"


def test_parse_patch_candidates_accepts_fenced_list() -> None:
    candidates = parse_patch_candidates(
        """```json
[
  {
    "ops": [
      {"op": "remove", "path": "/entities/npc_aldric/tags/0"}
    ]
  }
]
```"""
    )

    assert candidates[0].ops[0].op == "remove"


def test_parse_patch_candidates_rejects_natural_language() -> None:
    with pytest.raises(ValueError, match="patch output must"):
        parse_patch_candidates('"just change it"')
