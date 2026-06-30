"""Probe 3: int8 / quantization recall@k vs fp32 exact ground truth, + two-stage rerank.

Synthetic random vectors (worst case for quantization - no cluster structure).
Compares:
  - usearch f32 (ANN, approximate graph) recall@k vs exact
  - usearch bf16 (default) recall@k
  - usearch i8 recall@k
  - Python int8 (clip(round(x*127),-127,127)) exact-scan recall@k  [== sqlite-vec vec0 INT8 behaviour, brute force]
  - two-stage: i8/int8 coarse top k'=3k  -> fp32 rerank -> top k
"""
import numpy as np, time

NDIM = 1024
rng = np.random.default_rng(7)

def norm(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True); n[n == 0] = 1.0
    return (v / n).astype(np.float32)

N = 30000
NQ = 200
K = 10
db = norm(rng.standard_normal((N, NDIM)).astype(np.float32))
qs = norm(rng.standard_normal((NQ, NDIM)).astype(np.float32))

# Exact fp32 ground truth (brute force) — the reference
print(f"computing exact ground truth N={N} NQ={NQ} K={K} ...")
t = time.perf_counter()
gt = []
for q in qs:
    scores = db @ q
    top = np.argpartition(-scores, K)[:K]
    top = top[np.argsort(-scores[top])]
    gt.append(set(top.tolist()))
print(f"  ground truth in {time.perf_counter()-t:.2f}s")

def recall_at_k(pred_lists, k=K):
    rs = []
    for p, g in zip(pred_lists, gt):
        rs.append(len(set(p[:k]) & g) / k)
    return np.mean(rs)

# ---- Python int8 quantization (the design's clip(round(x*127)) ; == sqlite-vec vec0 INT8) ----
def quant_i8(v):
    return np.clip(np.round(v * 127), -127, 127).astype(np.int8)

db_i8 = quant_i8(db)
qs_i8 = quant_i8(qs)

print("\n=== Python int8 brute-force (== sqlite-vec INT8 column) ===")
t = time.perf_counter()
pred_i8 = []
pred_i8_3k = []
for i, q in enumerate(qs_i8):
    scores = db_i8.astype(np.int32) @ q.astype(np.int32)  # int8 dot
    order = np.argsort(-scores)
    pred_i8.append(order[:K].tolist())
    pred_i8_3k.append(order[:3 * K].tolist())
print(f"  scan {time.perf_counter()-t:.2f}s   recall@{K} = {recall_at_k(pred_i8):.4f}")

# Two-stage: int8 coarse k'=3K -> fp32 rerank -> top K
pred_2stage = []
for i, cand in enumerate(pred_i8_3k):
    cand = np.array(cand)
    s = db[cand] @ qs[i]            # fp32 rerank on candidate set
    re = cand[np.argsort(-s)][:K]
    pred_2stage.append(re.tolist())
print(f"  two-stage (i8 coarse 3k -> fp32 rerank top k):  recall@{K} = {recall_at_k(pred_2stage):.4f}")

# also try k'=5k and k'=10k
for mult in (5, 10, 20):
    pred_ms = []
    for i, q in enumerate(qs_i8):
        scores = db_i8.astype(np.int32) @ q.astype(np.int32)
        cand = np.argpartition(-scores, mult * K)[:mult * K]
        s = db[cand] @ qs[i]
        re = cand[np.argsort(-s)][:K]
        pred_ms.append(re.tolist())
    print(f"  two-stage (i8 coarse {mult}k -> fp32 rerank top k): recall@{K} = {recall_at_k(pred_ms):.4f}")

# ---- usearch dtypes (ANN graph) ----
from usearch.index import Index
keys = np.arange(N, dtype=np.uint64)
for dt in ['f32', 'bf16', 'i8']:
    idx = Index(ndim=NDIM, metric='cos', dtype=dt)
    t = time.perf_counter()
    idx.add(keys, db, threads=0)
    build = time.perf_counter() - t
    t = time.perf_counter()
    preds = []
    for q in qs:
        m = idx.search(q, K)
        preds.append([int(x) for x in m.keys])
    qt = (time.perf_counter() - t) / NQ * 1000
    print(f"\n=== usearch dtype={dt} (ANN) ===")
    print(f"  build {build:.2f}s, query {qt:.2f} ms/q, recall@{K} = {recall_at_k(preds):.4f}")

    # usearch two-stage: ANN coarse 3K -> fp32 rerank
    preds2 = []
    for i, q in enumerate(qs):
        m = idx.search(q, 3 * K)
        cand = np.array([int(x) for x in m.keys])
        s = db[cand] @ q
        re = cand[np.argsort(-s)][:K]
        preds2.append(re.tolist())
    print(f"  usearch {dt} coarse 3k -> fp32 rerank: recall@{K} = {recall_at_k(preds2):.4f}")

    # usearch with exact=True (brute force, no graph approximation)
    t = time.perf_counter()
    preds_ex = []
    for q in qs:
        m = idx.search(q, K, exact=True)
        preds_ex.append([int(x) for x in m.keys])
    qte = (time.perf_counter() - t) / NQ * 1000
    print(f"  usearch {dt} exact=True: {qte:.2f} ms/q, recall@{K} = {recall_at_k(preds_ex):.4f}")
