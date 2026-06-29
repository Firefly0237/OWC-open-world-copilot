"""Probe deep-nesting / huge-number robustness on JSON importer + field mapping doc."""
import sys, json
sys.path.insert(0, r"F:\openworld\src")
from pathlib import Path
from owcopilot.content.importers.json import JSONImporter

scr = Path(r"F:\openworld\scratchpad")

# 1) Deeply nested JSON value inside a content object's metadata
# Build deep JSON as a raw string so we don't recurse in json.dumps; the importer's json.loads
# is the thing under test for stack safety.
depth = 5000
deep_str = '{"id":"e1","name":"n","type":"concept","meta":' + '{"x":' * depth + '0' + '}' * depth + '}'
p = scr / "deep.json"
p.write_text(deep_str, encoding="utf-8")
try:
    objs = JSONImporter().parse(p)
    print("deep-nest parse OK, objs:", len(objs))
except RecursionError as e:
    print("deep-nest -> RecursionError (raw, ugly):", type(e).__name__)
except Exception as e:
    print("deep-nest ->", type(e).__name__, str(e)[:120])

# 2) Huge integer as id-adjacent / timeline_order (int overflow / perf)
big = {"kind": "quest", "id": "q_big", "title": "t", "timeline_order": 10**400}
p2 = scr / "big.json"
p2.write_text(json.dumps(big), encoding="utf-8")
try:
    from owcopilot.content.normalize import normalize_raw_objects
    b = normalize_raw_objects(JSONImporter().parse(p2))
    q = b.quests["q_big"]
    print("huge timeline_order accepted:", q.timeline_order is not None, "len(str)=", len(str(q.timeline_order)))
except Exception as e:
    print("huge-int ->", type(e).__name__, str(e)[:120])

# 3) emoji + surrogate-ish unicode in id (per-file kind -> filename)
emoji = {"kind": "entity", "id": "npc_💀🔥", "name": "x", "type": "npc"}
p3 = scr / "emoji.json"
p3.write_text(json.dumps(emoji, ensure_ascii=False), encoding="utf-8")
try:
    from owcopilot.content.normalize import normalize_raw_objects as nrm
    b = nrm(JSONImporter().parse(p3))
    print("emoji id entity accepted:", list(b.entities))
except Exception as e:
    print("emoji ->", type(e).__name__, str(e)[:120])
