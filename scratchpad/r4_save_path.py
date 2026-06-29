"""Does an ACCEPTED-but-weird id (DEL 0x7f, fullwidth slash) survive a real save->load roundtrip?"""
import sys, tempfile, os
sys.path.insert(0, r"F:\openworld\src")
from pathlib import Path
from owcopilot.content.importers.base import RawObject
from owcopilot.content.normalize import normalize_raw_objects
from owcopilot.content.store import ContentStore

def ro(kind, data):
    return RawObject(kind=kind, data=data, source_path="<chaos>", line=1)

tmp = Path(tempfile.mkdtemp(prefix="r4_save_"))
print("tmp:", tmp)

cases = {
    "del_char": "npc_a\x7fb",
    "fullwidth_slash": "npc_a／b",
    "fullwidth_dot": "npc_a．．b",
    "emoji": "npc_skull",  # ascii-safe stand-in; emoji tested separately
}
for label, eid in cases.items():
    try:
        b = normalize_raw_objects([ro("entity", {"id": eid, "name": "x", "type": "npc"})])
        store = ContentStore(tmp / label)
        store.save(b)
        files = sorted(p.name.encode("unicode_escape").decode() for p in (tmp/label/"world"/"entities").glob("*.json"))
        reloaded = ContentStore(tmp/label).load()
        ok = list(reloaded.entities) == list(b.entities)
        print(f"  {label:18} id={eid.encode('unicode_escape').decode()!r:18} -> files={files} roundtrip_ok={ok}")
    except Exception as e:
        print(f"  {label:18} -> {type(e).__name__}: {str(e)[:100]}")

# emoji id end to end (avoid printing raw emoji to GBK console)
b = normalize_raw_objects([ro("entity", {"id": "npc_\U0001f480", "name": "x", "type": "npc"})])
store = ContentStore(tmp/"emoji")
try:
    store.save(b)
    files = sorted(p.name.encode("unicode_escape").decode() for p in (tmp/"emoji"/"world"/"entities").glob("*.json"))
    print(f"  emoji id end-to-end -> files={files}")
except Exception as e:
    print(f"  emoji id end-to-end -> {type(e).__name__}: {str(e)[:100]}")

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print("done")
