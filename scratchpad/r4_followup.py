import sys
sys.path.insert(0, r"F:\openworld\src")
from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects

def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<chaos>", line=1)

print("### B6 detail: column 'id' (Indonesian) + 'en' ###")
b = normalize_raw_objects([ro("localized_text", {"text_key": "greeting", "en": "hello", "id": "halo"})])
for tid, lt in b.localized_texts.items():
    print(f"  id={tid!r} locale={lt.locale!r} text={lt.text!r}")
print("  -> # rows:", len(b.localized_texts), "(expected 2 if both kept; 'id' col silently dropped if 1)")

print()
print("### B6b: column 'id' as the ONLY translation column ###")
b = normalize_raw_objects([ro("localized_text", {"text_key": "greeting", "id": "halo"})])
print("  rows:", [(t.locale, t.text) for t in b.localized_texts.values()], "(empty => Indonesian translation lost silently)")

print()
print("### A2 detail: synthetic id colon chain becomes a dict key & file? ###")
b = normalize_raw_objects([ro("quest_event_ref", {"quest_id": "a:b", "event_id": "c:d"})])
for rid, ref in b.quest_event_refs.items():
    print(f"  ref id={rid!r}  quest_id={ref.quest_id!r} event_id={ref.event_id!r}")
print("  -> qer is stored in event_refs.jsonl (NOT {id}.json), so colon is path-safe")

print()
print("### DialogueRef.locale: does it get the iso-639-1 whitelist? ###")
b = normalize_raw_objects([ro("dialogue", {"id": "d1", "text_key": "k", "locale": "zz", "text": "x"})])
for d in b.dialogues.values():
    print(f"  dialogue locale={d.locale!r}  (note: dialogue.locale is NOT whitelisted - 'zz' kept as-is)")

print()
print("### locale with whitespace / case on localized_text actual locale field ###")
for loc in ["EN", " en ", "en_US", "zh_CN"]:
    b = normalize_raw_objects([ro("localized_text", {"text_key": "k", "locale": loc, "text": "hi"})])
    locs = [t.locale for t in b.localized_texts.values()]
    print(f"  locale field {loc!r} -> stored locales {locs}")
