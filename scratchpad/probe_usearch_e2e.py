"""Probe 1: usearch end-to-end on Windows cp313, $0 synthetic vectors.

Covers: import, Index(ndim,metric,dtype), add, save->disk, load/view(mmap),
search top-k, int8/f16 quantization (dtype), get vector back, remove (delete).
Records exact API shapes for design.
"""
import os, time, tempfile, numpy as np
from usearch.index import Index

NDIM = 1024
rng = np.random.default_rng(42)

def norm(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return (v / n).astype(np.float32)

print("=== STEP 1: build f32 Index(ndim=1024, metric='cos') ===")
idx = Index(ndim=NDIM, metric="cos")  # default dtype f32
print("  metric:", idx.metric, "dtype:", idx.dtype, "ndim:", idx.ndim)

print("=== STEP 2: add a batch of vectors (integer keys) ===")
N = 2000
vecs = norm(rng.standard_normal((N, NDIM)).astype(np.float32))
keys = np.arange(N, dtype=np.uint64)
t = time.perf_counter()
idx.add(keys, vecs)
print(f"  added {N} vecs in {time.perf_counter()-t:.3f}s; size={idx.size}, count={idx.count}")

print("=== STEP 3: search top-k ===")
q = vecs[123]  # known vector -> should self-retrieve at rank0
m = idx.search(q, 5)
print("  type:", type(m).__name__)
print("  keys:", m.keys.tolist())
print("  distances:", [round(float(x),4) for x in m.distances])
print("  rank0 key == query key 123 ?", int(m.keys[0]) == 123)
# Matches object shape
print("  m.keys dtype:", m.keys.dtype, " m.distances dtype:", m.distances.dtype)

print("=== STEP 4: save to disk + load/view (mmap, not full into RAM) ===")
d = tempfile.mkdtemp(prefix="usearch_")
p = os.path.join(d, "f32.usearch")
idx.save(p)
sz = os.path.getsize(p)
print(f"  saved file: {sz/1e6:.2f} MB at {p}")

# load = read fully; view = mmap on-disk (no full load)
idx_view = Index(ndim=NDIM, metric="cos")
idx_view.view(p)
print("  view() ok; size after view:", idx_view.size)
mv = idx_view.search(q, 3)
print("  search-after-view keys:", mv.keys.tolist(), "rank0==123?", int(mv.keys[0]) == 123)

# restore() classmethod (view=True) convenience
idx_restored = Index.restore(p, view=True)
print("  restore(view=True) size:", idx_restored.size, "type:", type(idx_restored).__name__)

print("=== STEP 5: get vector back (vector_for) ===")
got = idx.get(np.uint64(123))
print("  get(123) shape:", None if got is None else got.shape, "dtype:", None if got is None else got.dtype)
print("  cos(stored, original) =", float(np.dot(norm(got.reshape(-1))[0] if got.ndim>1 else norm(got), vecs[123])) if got is not None else "N/A")

print("=== STEP 6: remove (delete) ===")
before = idx.count
idx.remove(np.uint64(123))
print(f"  count {before} -> {idx.count}; contains(123)? {idx.contains(np.uint64(123))}")

print("=== STEP 7: int8 quantized index (dtype='i8') ===")
idx8 = Index(ndim=NDIM, metric="cos", dtype="i8")
print("  dtype:", idx8.dtype)
t = time.perf_counter()
idx8.add(keys, vecs)
print(f"  added {N} into i8 index in {time.perf_counter()-t:.3f}s; size={idx8.size}")
m8 = idx8.search(q, 5)
print("  i8 search keys:", m8.keys.tolist(), "rank0==123?", int(m8.keys[0]) == 123)
p8 = os.path.join(d, "i8.usearch")
idx8.save(p8)
print(f"  i8 file: {os.path.getsize(p8)/1e6:.2f} MB  (vs f32 {sz/1e6:.2f} MB)")

print("=== STEP 8: f16 quantized index (dtype='f16') ===")
idx16 = Index(ndim=NDIM, metric="cos", dtype="f16")
idx16.add(keys, vecs)
m16 = idx16.search(q, 5)
p16 = os.path.join(d, "f16.usearch")
idx16.save(p16)
print("  f16 search keys:", m16.keys.tolist(), "rank0==123?", int(m16.keys[0]) == 123)
print(f"  f16 file: {os.path.getsize(p16)/1e6:.2f} MB")

print("\nALL STEPS COMPLETED OK")
