"""Chaos repro: probe normalize_raw_objects for malformed-input gaps that bypass R2 ID hardening."""
import sys, traceback
sys.path.insert(0, r"F:\openworld\src")

from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects


def probe(label, raw_objs):
    print("=" * 70)
    print("CASE:", label)
    try:
        bundle = normalize_raw_objects(raw_objs)
        # dump what got produced for the relevant kind
        ids = {
            "entities": list(bundle.entities),
            "quests": list(bundle.quests),
            "quest_event_refs": list(bundle.quest_event_refs),
            "regions": list(bundle.regions),
            "pois": list(bundle.pois),
            "dialogues": list(bundle.dialogues),
            "localized_texts": list(bundle.localized_texts),
            "terms": list(bundle.terms),
            "style_guides": list(bundle.style_guides),
        }
        nonempty = {k: v for k, v in ids.items() if v}
        print("  -> OK, produced:", nonempty)
        # show the actual id reprs to reveal control chars / mangling
        for k, v in nonempty.items():
            for i in v:
                print(f"     {k} id repr = {i!r}")
    except Exception as e:
        print(f"  -> RAISED {type(e).__name__}: {e}")


def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<chaos>", line=1)


# --- GAP 1: quest_event_ref id as non-str (bypasses _assert_id_is_str_or_none) ---
probe("quest_event_ref id=list", [ro("quest_event_ref", {"id": ["a", "b"], "quest_id": "q1", "event_id": "e1"})])
probe("quest_event_ref id=dict", [ro("quest_event_ref", {"id": {"x": 1}, "quest_id": "q1", "event_id": "e1"})])
probe("quest_event_ref id=True", [ro("quest_event_ref", {"id": True, "quest_id": "q1", "event_id": "e1"})])

# --- GAP 1b: quest_event_ref id with control chars (bypasses _require_valid_id) ---
probe("quest_event_ref id=NUL", [ro("quest_event_ref", {"id": "evt\x00ref", "quest_id": "q1", "event_id": "e1"})])
probe("quest_event_ref id=path-traversal", [ro("quest_event_ref", {"id": "../../etc/passwd", "quest_id": "q1", "event_id": "e1"})])
probe("quest_event_ref id=slash", [ro("quest_event_ref", {"id": "a/b:c", "quest_id": "q1", "event_id": "e1"})])

# --- GAP 1c: quest_event_ref quest_id/event_id with control chars (used to build default id) ---
probe("quest_event_ref quest_id=NUL no-id", [ro("quest_event_ref", {"quest_id": "q\x001", "event_id": "e1"})])

# --- GAP 2: style_guide id as non-str / control chars (bypasses both) ---
probe("style_guide id=list", [ro("style_guide", {"id": ["a"], "body": "x"})])
probe("style_guide id=dict", [ro("style_guide", {"id": {"k": "v"}, "body": "x"})])
probe("style_guide id=NUL", [ro("style_guide", {"id": "sg\x00", "body": "x"})])
probe("style_guide id=traversal", [ro("style_guide", {"id": "../../../boom", "body": "x"})])

# --- GAP 3: duplicate quest_event_ref / style_guide ids in same batch ---
probe("dup quest_event_ref id", [
    ro("quest_event_ref", {"id": "dup", "quest_id": "q1", "event_id": "e1"}),
    ro("quest_event_ref", {"id": "dup", "quest_id": "q2", "event_id": "e2"}),
])

# --- GAP 4: localized_text id mangling (uses slug_id, never validates) ---
probe("localized_text id=list", [ro("localized_text", {"id": ["a"], "text_key": "k", "locale": "en", "text": "hi"})])
probe("localized_text id=dict", [ro("localized_text", {"id": {"x": 1}, "text_key": "k", "locale": "en", "text": "hi"})])

# --- GAP 5: relation source/target with control chars ---
probe("relation source=NUL", [ro("relation", {"source": "a\x00", "target": "b", "kind": "knows"})])

print("\nDONE")
