import sys
sys.path.insert(0, r"F:\openworld\src")
from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects

def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<chaos>", line=1)

print("ASYMMETRY: locale WHITELIST applies to column-detection only, not to explicit 'locale' field")
print("-"*70)
# explicit locale field can be ANYTHING (no whitelist):
for badloc in ["zz", "qq", "../slip", "xx-yy", "NOTALOCALE", ""]:
    b = normalize_raw_objects([ro("localized_text", {"text_key": "k", "locale": badloc, "text": "t"})])
    locs = [(t.locale) for t in b.localized_texts.values()]
    print(f"  explicit locale={badloc!r:14} -> stored {locs}")

print()
print("But a COLUMN named the same is whitelist-filtered:")
for col in ["zz", "qq", "xx"]:
    b = normalize_raw_objects([ro("localized_text", {"text_key": "k", col: "ghost"})])
    locs = [(t.locale) for t in b.localized_texts.values()]
    print(f"  column {col!r:6} -> stored {locs}  (whitelist drops it)")

print()
print("CONSISTENCY CHECK: does a VALID iso col + the explicit locale path agree?")
b = normalize_raw_objects([ro("localized_text", {"text_key": "k", "fr": "bonjour", "de": "hallo"})])
print("  fr+de columns ->", sorted((t.locale, t.text) for t in b.localized_texts.values()))
