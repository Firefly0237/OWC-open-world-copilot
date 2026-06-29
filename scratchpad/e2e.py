"""End-to-end: ingest malformed quest_event_ref/style_guide via CLI path, with --write, then inspect store."""
import sys, json, shutil
from pathlib import Path
sys.path.insert(0, r"F:\openworld\src")

root = Path(r"F:\openworld\scratchpad\chaosroot")
if root.exists():
    shutil.rmtree(root)
root.mkdir(parents=True)

from owcopilot.cli.main import main

print("=== DRY RUN ===")
rc = main([
    "ingest",
    "--content-root", str(root),
    "--input", r"F:\openworld\scratchpad\qer.jsonl",
])
print("exit:", rc)

print("\n=== WRITE ===")
rc = main([
    "ingest", "--write",
    "--content-root", str(root),
    "--input", r"F:\openworld\scratchpad\qer.jsonl",
])
print("exit:", rc)

print("\n=== FILES ON DISK (look for traversal/mangled ids persisted) ===")
for p in sorted(root.rglob("*")):
    if p.is_file():
        print(" ", p.relative_to(root))
