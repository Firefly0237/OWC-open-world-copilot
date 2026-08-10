"""Shape + provenance guard for the narrative-complexity profiles (N2a deliverable).

Asserts on the local research files only —
``benchmarks/evidence/narrative-complexity/complexity-profiles.json`` and its evidence doc —
with the
stdlib alone (json/pathlib/re). No model, no network, no business-logic copy: this
pins that the three tiers stay well-formed and that every profile traces back to a declared source,
so N2b can read ``profiles[tier].params[name]`` without a mapping shim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_ROOT = _REPO_ROOT / "benchmarks" / "evidence" / "narrative-complexity"
_JSON_PATH = _PROFILE_ROOT / "complexity-profiles.json"
_DOC_PATH = _PROFILE_ROOT / "source-analysis.md"
_N1_REPORT = _REPO_ROOT / "benchmarks" / "ink_losslessness.md"

_TIERS = {"support", "chapter", "multiline"}
# The 10 params N2b consumes; names/semantics == content/models.py fields.
_PARAM_NAMES = {
    "quest_count",
    "stages_per_quest",
    "branch_factor",
    "convergence_rate",
    "logic_var_count",
    "logic_var_ratio",
    "cross_quest_ref_density",
    "dialogue_tree_depth",
    "dialogue_tree_width",
    "conditional_choice_ratio",
}
_UNIT_INTERVAL = {"convergence_rate", "conditional_choice_ratio"}
_URL_RE = re.compile(r"^https?://")


def _load() -> dict[str, Any]:
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


def _is_range(value: Any) -> bool:
    """A 2-number ascending array [lo, hi] (booleans, which subclass int, are rejected)."""
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
        and value[0] <= value[1]
    )


def test_loads_and_complete() -> None:
    data = _load()
    profiles = data["profiles"]
    assert set(profiles) == _TIERS

    for tier, prof in profiles.items():
        params = prof["params"]
        # Exactly the 10 params, nothing more — so iterating params never trips on a stray key.
        assert set(params) == _PARAM_NAMES, tier

        for name in _PARAM_NAMES - {"logic_var_ratio"}:
            rng = params[name]
            assert _is_range(rng), (tier, name, rng)
            if name in _UNIT_INTERVAL:
                assert 0.0 <= rng[0] <= rng[1] <= 1.0, (tier, name, rng)

        ratio = params["logic_var_ratio"]
        assert set(ratio) == {"bool", "int", "enum"}, tier
        for kind, sub in ratio.items():
            assert _is_range(sub), (tier, kind, sub)
            assert 0.0 <= sub[0] <= sub[1] <= 1.0, (tier, kind, sub)


def test_every_stat_sourced() -> None:
    data = _load()
    source_ids = set(data["sources"])
    for src in data["sources"].values():
        assert _URL_RE.match(src["url"]), src["url"]

    for tier, prof in data["profiles"].items():
        derived = prof["derived_from"]
        assert derived, tier  # non-empty
        assert set(derived) <= source_ids, (tier, derived)
        # Extrapolation flags, if present, must name real params — never a phantom knob.
        assert set(prof.get("assumed", {})) <= _PARAM_NAMES, (tier, prof.get("assumed"))


def test_doc_cites_sources() -> None:
    data = _load()
    doc = _DOC_PATH.read_text(encoding="utf-8")
    for src in data["sources"].values():
        assert src["url"] in doc, src["url"]
    for prof in data["profiles"].values():
        assert prof["label"] in doc, prof["label"]


def test_support_anchor_reproducible() -> None:
    # Asserts the provenance link only — does NOT re-derive the N1 counts.
    anchor = _load()["profiles"]["support"]["anchor"]
    assert "benchmarks/ink_losslessness.md" in anchor
    assert _N1_REPORT.exists()
