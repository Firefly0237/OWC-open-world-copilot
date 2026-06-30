"""Probe 4: sharding spike.

(a) sqlite-vec vec0 PARTITION KEY: can a world_id/version column prune the scan to one shard
    so per-query N drops to current-version size? Measure KNN latency scoped to one partition
    vs full table.
(b) usearch: no metadata/partition column. Confirm it needs one index file per shard, and measure
    per-shard open/query.
"""
import os, time, tempfile, sqlite3, numpy as np
import sqlite_vec
from usearch.index import Index

NDIM = 1024
rng = np.random.default_rng(5)
def norm(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True); n[n==0]=1.0
    return (v/n).astype(np.float32)

# 50 worlds x 2000 rows each = 100k total; query should only need 1 world (2000)
NW = 50
PER = 2000
N = NW * PER
db = norm(rng.standard_normal((N, NDIM)).astype(np.float32))
worlds = np.repeat(np.arange(NW), PER)
q = db[12345]
qb = q.tobytes()

d = tempfile.mkdtemp(prefix="shard_")

print("=== (a) sqlite-vec PARTITION KEY ===")
conn = sqlite3.connect(os.path.join(d, "p.sqlite"))
conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
try:
    conn.execute(f"CREATE VIRTUAL TABLE vp USING vec0(world TEXT PARTITION KEY, ref TEXT, embedding FLOAT[{NDIM}])")
    part_ok = True
except Exception as e:
    part_ok = False; print("  PARTITION KEY create FAILED:", e)
if part_ok:
    t = time.perf_counter()
    conn.executemany("INSERT INTO vp(world, ref, embedding) VALUES (?,?,?)",
                     [(f"w{int(worlds[i])}", str(i), db[i].tobytes()) for i in range(N)])
    conn.commit()
    print(f"  inserted {N} across {NW} partitions in {time.perf_counter()-t:.2f}s")
    # scoped query: one world only
    t = time.perf_counter()
    for _ in range(20):
        rows = conn.execute("SELECT ref FROM vp WHERE embedding MATCH ? AND k=10 AND world='w25' ORDER BY distance",
                            (qb,)).fetchall()
    scoped = (time.perf_counter()-t)/20*1000
    # full-table query (no world filter) -- needs k passed; partitioned table requires per-partition?
    try:
        t = time.perf_counter()
        for _ in range(20):
            rows_all = conn.execute("SELECT ref FROM vp WHERE embedding MATCH ? AND k=10 ORDER BY distance",
                                    (qb,)).fetchall()
        full = (time.perf_counter()-t)/20*1000
        print(f"  scoped (world='w25', N={PER}) = {scoped:.1f} ms/q   full-scan (all {N}) = {full:.1f} ms/q")
        print(f"  speedup from partition pruning ~ {full/scoped:.1f}x")
    except Exception as e:
        print(f"  scoped (world='w25', N={PER}) = {scoped:.1f} ms/q ; full unscoped query errored: {e}")
conn.close()

print("\n=== (b) usearch per-shard (no partition column) ===")
# one index per world
shard_dir = os.path.join(d, "shards"); os.makedirs(shard_dir)
t = time.perf_counter()
for w in range(NW):
    sel = worlds == w
    idx = Index(ndim=NDIM, metric='cos', dtype='f32')
    idx.add(np.arange(sel.sum(), dtype=np.uint64), db[sel])
    idx.save(os.path.join(shard_dir, f"w{w}.usearch"))
print(f"  built+saved {NW} shard indices in {time.perf_counter()-t:.2f}s")
# query one shard via view (mmap)
t = time.perf_counter()
for _ in range(20):
    iv = Index(ndim=NDIM, metric='cos', dtype='f32'); iv.view(os.path.join(shard_dir, "w25.usearch"))
    iv.search(q, 10)
print(f"  open(view)+query one shard (N={PER}) = {(time.perf_counter()-t)/20*1000:.2f} ms/q (incl. mmap open)")
# query with index kept open
iv = Index(ndim=NDIM, metric='cos', dtype='f32'); iv.view(os.path.join(shard_dir, "w25.usearch"))
t = time.perf_counter()
for _ in range(50):
    iv.search(q, 10)
print(f"  query one shard, index kept open = {(time.perf_counter()-t)/50*1000:.3f} ms/q")
