"""Probe localized_text dict/list id producing spurious rows + check export path for traversal ids."""
import sys
sys.path.insert(0, r"F:\openworld\src")
from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects, _localized_texts_from_raw

def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<x>", line=1)

# Why did localized_text id=dict produce loc_k_en AND loc_k_id?
raw = ro("localized_text", {"id": {"x": 1}, "text_key": "k", "locale": "en", "text": "hi"})
rows = _localized_texts_from_raw(raw)
print("localized_text id=dict rows:")
for r in rows:
    print("   id=%r locale=%r text=%r" % (r.id, r.locale, r.text))
print()

# Single clean locale, no id -> how many rows?
raw2 = ro("localized_text", {"text_key": "k", "locale": "en", "text": "hi"})
print("clean single-locale rows:", [(r.id, r.locale) for r in _localized_texts_from_raw(raw2)])
print()

# A key that LOOKS like a locale but is garbage -> injected as a locale row?
raw3 = ro("localized_text", {"text_key": "k", "zz": "garbage", "qq": "more"})
print("garbage 2-letter keys treated as locales:", [(r.id, r.locale, r.text) for r in _localized_texts_from_raw(raw3)])
