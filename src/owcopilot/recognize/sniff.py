"""Guess a source format from a filename + a content sample, so the UI can pre-select (overridable).

Extension wins when it's unambiguous; otherwise we look at the content's shape. JSON is the tricky
one — a `.json` could be an articy export, a UE DataTable, a Unity asset, or just a table — so we
peek at structural markers. The guess is always overridable: this only seeds the dropdown.
"""

from __future__ import annotations

import json

_EXT = {
    ".csv": "table",
    ".tsv": "table",
    ".xlsx": "table",
    ".xlsm": "table",
    ".ink": "ink",
    ".yarn": "yarn",
}


def _sniff_json(data: object) -> str:
    if isinstance(data, dict):
        if "Packages" in data or "GlobalVariables" in data:
            return "articy"
        # a single dict could be a Unity asset or a keyed table; default to table
        if "m_Name" in data or any(k.startswith("m_") for k in data):
            return "unity"
        return "table"
    if isinstance(data, list) and data and isinstance(data[0], dict):
        sample = data[0]
        if any(k.startswith("m_") for k in sample):
            return "unity"
        if "Name" in sample and any(
            isinstance(v, dict) and "RowName" in v for v in sample.values()
        ):
            return "ue"
        return "table"
    return "table"


def sniff_source_format(filename: str, sample: str) -> str:
    """Return a best-guess source_format. Falls back to ``table`` when unsure."""
    lower = filename.lower()
    for ext, fmt in _EXT.items():
        if lower.endswith(ext):
            return fmt

    text = sample.lstrip()
    if lower.endswith(".json") or text.startswith(("{", "[")):
        try:
            return _sniff_json(json.loads(sample))
        except (ValueError, TypeError):
            pass  # malformed JSON — fall through to text heuristics

    head = "\n".join(text.splitlines()[:40])
    if "title:" in head and "---" in head:
        return "yarn"
    if "<<jump" in head or "<<declare" in head:
        return "yarn"
    if "=== " in head or "\nVAR " in f"\n{head}" or "-> " in head:
        return "ink"
    return "table"
