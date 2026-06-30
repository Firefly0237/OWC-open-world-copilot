"""Probe 2: scale comparison usearch ANN vs sqlite-vec brute force.

For N in {50000, 300000} x 1024:
  - usearch f32 ANN: build time, query latency, on-disk file size, in-RAM index memory, recall@10
  - usearch with view() (mmap): query latency without full load
  - sqlite-vec brute (== current SqliteVecBackend): KNN latency (recompute exact dot)
Clustered synthetic data so ANN recall is realistic.
Run with: python probe_scale_usearch.py <N>
"""
import os, sys, time, tempfile, sqlite3, numpy as np
from usearch.index import Index

NDIM = 1024
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
NQ = 50
K = 10
rng = np.random.default_rng(3)

def norm(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True); n[n==0]=1.0
    return (v/n).astype(np.float32)

def proc_rss_mb():
    # Windows RSS via ctypes (no psutil dependency)
    import ctypes, ctypes.wintypes as wt
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    p = PMC(); p.cb = ctypes.sizeof(PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb)
    return (p.WorkingSetSize / 1e6) if ok else -1.0

print(f"### N={N} dim={NDIM} ###")
NCLUST = max(50, N // 150)
cent = norm(rng.standard_normal((NCLUST, NDIM)).astype(np.float32))
assign = rng.integers(0, NCLUST, N)
db = norm(cent[assign] + 0.35 * rng.standard_normal((N, NDIM)).astype(np.float32))
qa = rng.integers(0, NCLUST, NQ)
qs = norm(cent[qa] + 0.35 * rng.standard_normal((NQ, NDIM)).astype(np.float32))
print(f"data RSS after generating db: {proc_rss_mb():.0f} MB (db matrix alone = {db.nbytes/1e6:.0f} MB)")

# exact GT
gt = []
for q in qs:
    s = db @ q; t = np.argpartition(-s, K)[:K]; t = t[np.argsort(-s[t])]; gt.append(set(t.tolist()))
def rec(preds, k=K): return np.mean([len(set(p[:k]) & g)/k for p,g in zip(preds, gt)])

keys = np.arange(N, dtype=np.uint64)

# ---------- usearch f32 ANN ----------
rss0 = proc_rss_mb()
idx = Index(ndim=NDIM, metric='cos', dtype='f32')
t = time.perf_counter(); idx.add(keys, db, threads=0); bt = time.perf_counter()-t
idx_mem = idx.memory_usage / 1e6
rss1 = proc_rss_mb()
t = time.perf_counter()
preds = [[int(x) for x in idx.search(q, K).keys] for q in qs]
qt = (time.perf_counter()-t)/NQ*1000
d = tempfile.mkdtemp(prefix="us_scale_"); p = os.path.join(d, "f32.usearch")
idx.save(p); fsz = os.path.getsize(p)/1e6
print(f"\n[usearch f32 ANN]  build={bt:.2f}s  query={qt:.3f} ms/q  recall@{K}={rec(preds):.4f}")
print(f"  index in-RAM (idx.memory_usage)={idx_mem:.0f} MB  proc RSS delta={rss1-rss0:.0f} MB  disk file={fsz:.0f} MB")
# coarse 3k -> fp32 rerank (we still have db in RAM here; in prod rerank vecs come from blob cache)
pr = []
for i, q in enumerate(qs):
    c = np.array([int(x) for x in idx.search(q, 3*K).keys]); s = db[c] @ q
    pr.append(c[np.argsort(-s)][:K].tolist())
print(f"  coarse 3k -> fp32 rerank recall@{K}={rec(pr):.4f}")

# ---------- usearch view() mmap query (free the in-RAM index, reopen via mmap) ----------
idx.reset()
idx_v = Index(ndim=NDIM, metric='cos', dtype='f32'); idx_v.view(p)
t = time.perf_counter()
preds_v = [[int(x) for x in idx_v.search(q, K).keys] for q in qs]
qtv = (time.perf_counter()-t)/NQ*1000
print(f"  view()/mmap query={qtv:.3f} ms/q  recall@{K}={rec(preds_v):.4f}  (index not fully in RAM)")

# ---------- sqlite-vec brute force (== current SqliteVecBackend) ----------
import sqlite_vec
dbf = os.path.join(d, "vec.sqlite")
conn = sqlite3.connect(dbf); conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
conn.execute(f"CREATE VIRTUAL TABLE v USING vec0(ref TEXT PRIMARY KEY, embedding FLOAT[{NDIM}])")
t = time.perf_counter()
conn.executemany("INSERT INTO v(ref, embedding) VALUES (?, ?)",
                 [(str(i), db[i].tobytes()) for i in range(N)])
conn.commit(); ins = time.perf_counter()-t
# brute KNN exactly like SqliteVecBackend.search: k=count, recompute dot
def svec_search(q, k):
    # plain full scan (no MATCH; sqlite-vec caps MATCH k at 4096) -> exact dot in python.
    rows = conn.execute("SELECT ref, embedding FROM v").fetchall()
    scored = [(r[0], float(np.frombuffer(r[1], dtype=np.float32) @ q)) for r in rows]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [int(r) for r, _ in scored[:k]]
t = time.perf_counter()
preds_sv = [svec_search(q, K) for q in qs]
svq = (time.perf_counter()-t)/NQ*1000
svsz = os.path.getsize(dbf)/1e6
print(f"\n[sqlite-vec brute fp32]  insert={ins:.2f}s  query={svq:.1f} ms/q  recall@{K}={rec(preds_sv):.4f}  disk={svsz:.0f} MB")
conn.close()
