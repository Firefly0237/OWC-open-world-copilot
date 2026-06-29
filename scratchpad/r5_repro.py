"""R5 chaos repro: write-boundary id invariant + snapshot traversal + locale warnings."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from owcopilot.content.snapshot import load_snapshot, write_snapshot
from owcopilot.content.store import ContentStore
from owcopilot.content.models import ContentBundle, QuestEventReference, QuestEventRefKind, Term, StyleGuide
from owcopilot.content.importers.base import RawObject
from owcopilot.content import normalize


def hr(t): print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# ---------------------------------------------------------------------------
hr("A1. snapshot traversal via load_snapshot(snapshot_id=...)")
import tempfile, os
root = Path(tempfile.mkdtemp(prefix="r5snap_"))
store = ContentStore(root)
# create a sentinel .json OUTSIDE the .snapshots dir we want to try to read
secret = root / "secret.json"
secret.write_text('{"bundle": {"terms": {"PWNED": {"id":"PWNED","canonical":"leaked"}}}}', encoding="utf-8")
# also write one inside a parent dir
parent_secret = root.parent / "r5_outside_secret.json"
parent_secret.write_text('{"bundle": {"terms": {"OUTSIDE": {"id":"OUTSIDE","canonical":"escaped"}}}}', encoding="utf-8")
store.save(ContentBundle())  # ensure root exists
for sid in ["../secret", "..\\secret", "../r5_outside_secret", "..\\..\\r5_outside_secret"]:
    try:
        b = load_snapshot(store, sid)
        if b is not None and b.terms:
            print(f"  TRAVERSAL HIT sid={sid!r} -> leaked terms: {list(b.terms)}")
        else:
            print(f"  sid={sid!r} -> {'None (no file)' if b is None else 'empty bundle'}")
    except Exception as e:
        print(f"  sid={sid!r} -> {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
hr("A2. write-boundary: jsonl/aggregate keys with traversal/control chars")
root2 = Path(tempfile.mkdtemp(prefix="r5agg_"))
store2 = ContentStore(root2)
b = ContentBundle()
# term id with traversal - goes through _write_terms (aggregate), NOT _write_json_dir
try:
    b.terms["../../evil"] = Term(id="../../evil", canonical="x")
    b.style_guides["..\\esc"] = StyleGuide(id="..\\esc", body="x")
    b.quest_event_refs["a/b/c"] = QuestEventReference(id="a/b/c", quest_id="q", event_id="e", ref_kind=QuestEventRefKind.MENTIONS_EVENT)
    store2.save(b)
    print("  save() OK (aggregate writers bypass _validate_id_chars)")
    # do these keys reach any filename?
    import os
    created = []
    for dp, dn, fn in os.walk(root2):
        for f in fn:
            created.append(os.path.relpath(os.path.join(dp, f), root2))
    print("  files created:", created)
    # roundtrip
    rb = store2.load()
    print("  roundtrip term keys:", list(rb.terms))
    print("  roundtrip style keys:", list(rb.style_guides))
    print("  roundtrip qer keys:", list(rb.quest_event_refs))
except Exception as e:
    print(f"  save() raised {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
hr("A3. write-boundary: per-file dict writers DO reject traversal id")
root3 = Path(tempfile.mkdtemp(prefix="r5dir_"))
store3 = ContentStore(root3)
from owcopilot.content.models import Quest
b3 = ContentBundle()
b3.quests["../../escape"] = Quest(id="../../escape", title="t")
try:
    store3.save(b3)
    print("  BUG: save() accepted traversal quest id")
except ValueError as e:
    print(f"  OK rejected: {e}")


# ---------------------------------------------------------------------------
hr("B.误拒: legal synthetic colon qer id NOT rejected by write boundary")
root4 = Path(tempfile.mkdtemp(prefix="r5qer_"))
store4 = ContentStore(root4)
b4 = ContentBundle()
b4.quest_event_refs["q1:e1:mentions_event"] = QuestEventReference(
    id="q1:e1:mentions_event", quest_id="q1", event_id="e1", ref_kind=QuestEventRefKind.MENTIONS_EVENT)
try:
    store4.save(b4)
    rb4 = store4.load()
    print("  OK colon qer id survived save+load:", list(rb4.quest_event_refs))
except Exception as e:
    print(f"  误拒 BUG: {type(e).__name__}: {e}")

# legal boundary ids via normalize synthetic path
hr("B2. normalize: legal qer synthetic colon id through ingest")
raw = RawObject(kind="quest_event_ref", data={"quest_id": "q1", "event_id": "e1", "ref_kind": "mentions_event"}, source_path="x.csv")
bun = normalize.normalize_raw_objects([raw])
print("  synthetic qer id:", list(bun.quest_event_refs))
# explicit colon id should be REJECTED
raw2 = RawObject(kind="quest_event_ref", data={"id": "explicit:colon", "quest_id": "q", "event_id": "e"}, source_path="x.csv")
try:
    normalize.normalize_raw_objects([raw2])
    print("  NOTE: explicit colon qer id ACCEPTED (should be rejected)")
except ValueError as e:
    print(f"  OK explicit colon rejected: {e}")


# ---------------------------------------------------------------------------
hr("C. locale warning: Indonesian 'id' column + explicit locale whitelist")

def cap(fn):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = fn()
    return out, [str(x.message) for x in w]

# C1: 'id'-only localized_text row, no other locale -> SHOULD warn (value vanishes)
def c1():
    r = RawObject(kind="localized_text", data={"text_key": "hello", "id": "selamat"}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c1)
print(f"  C1 id-only row: localized_texts={len(out.localized_texts)} warnings={len(ws)}")
for m in ws: print("     warn:", m[:90])

# C2: 'id' present BUT real locale data also present -> should NOT warn about id
def c2():
    r = RawObject(kind="localized_text", data={"text_key": "hello", "id": "row1", "en": "Hi", "zh": "你好"}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c2)
print(f"  C2 id+locale row: localized_texts={len(out.localized_texts)} warnings={len(ws)} (expect 0)")
for m in ws: print("     warn:", m[:90])

# C3: explicit locale='zz' -> should warn (not ISO)
def c3():
    r = RawObject(kind="localized_text", data={"text_key": "x", "locale": "zz", "text": "v"}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c3)
print(f"  C3 explicit zz: rows={len(out.localized_texts)} warnings={len(ws)} (expect 1)")

# C4: explicit locale='zh-CN' -> should NOT warn
def c4():
    r = RawObject(kind="localized_text", data={"text_key": "x", "locale": "zh-CN", "text": "v"}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c4)
print(f"  C4 explicit zh-CN: rows={len(out.localized_texts)} warnings={len(ws)} (expect 0)")

# C5: dialogue with explicit locale='xx' -> should warn
def c5():
    r = RawObject(kind="dialogue", data={"text_key": "k", "locale": "xx"}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c5)
print(f"  C5 dialogue locale xx: warnings={len(ws)} (expect 1)")

# C6: 'id' as a real Indonesian translation but row HAS a 'name' key making it look like entity? n/a
# C6: localized row with ONLY reserved-locale key 'id' but value empty -> no warn
def c6():
    r = RawObject(kind="localized_text", data={"text_key": "x", "id": ""}, source_path="x.csv")
    return normalize.normalize_raw_objects([r])
out, ws = cap(c6)
print(f"  C6 empty id value: warnings={len(ws)} (expect 0 - empty not imported anyway)")

print("\nDONE")
