"""E2E proof: a traversal id from the recognize/review path escapes the content dir on save,
because id-char validation lives ONLY in normalize._resolve_id, which these paths bypass."""
import sys, tempfile, shutil
sys.path.insert(0, r"F:\openworld\src")
from pathlib import Path
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore

tmp = Path(tempfile.mkdtemp(prefix="r4_trav_"))
content_root = tmp / "myworld" / "content"
content_root.mkdir(parents=True)
print("content_root:", content_root)

# Simulate: foreign engine file -> ProposedEntity(id=...) -> plan_to_bundle -> review-approve
# -> ContentBundle.model_validate -> content_store.save. The model has NO id validator, so a
# traversal id is accepted and turned into a filename.

# 1) does the MODEL accept a traversal id?  (normalize is bypassed here)
try:
    ent = Entity(id="../../../escaped", name="evil", type=EntityType.NPC)
    print("Entity model accepted traversal id:", repr(ent.id), "  <<< no model-level guard")
except Exception as e:
    print("Entity model rejected:", e)

# 2) does ContentBundle.model_validate accept it? (the review-apply path)
seed = ContentBundle()
seed.add_entity(ent)
roundtrip = ContentBundle.model_validate(seed.model_dump(mode="json"))
print("ContentBundle.model_validate kept id:", repr(list(roundtrip.entities)[0]))

# 3) does store.save() write OUTSIDE the entities dir?
store = ContentStore(content_root)
before = set(p for p in tmp.rglob("*") if p.is_file())
store.save(roundtrip)
after = set(p for p in tmp.rglob("*") if p.is_file())
new_files = sorted(after - before)
print("\nFiles written:")
for f in new_files:
    rel = f.relative_to(tmp)
    escaped = "world" + chr(92) + "entities" not in str(f).replace("/", chr(92)) or ".." in str(f)
    print("  ", rel)

# explicit escape check: did anything land outside content_root?
escapees = [f for f in after if content_root.resolve() not in f.resolve().parents and content_root.resolve() != f.resolve().parent and "myworld" in str(f)]
outside_content = [f for f in after - before if content_root.resolve() not in f.resolve().parents]
print("\n*** Files written OUTSIDE content_root (path escape):")
for f in outside_content:
    print("   ESCAPED ->", f.resolve())
if not outside_content:
    print("   (none - id was contained)")

shutil.rmtree(tmp, ignore_errors=True)
