"""Probe 3b: recall on CLUSTERED vectors (realistic embedding manifold structure).

Random iid vectors are an adversarial worst case for HNSW (all ~equidistant).
Real bge-m3 embeddings have cluster/manifold structure. Generate clustered
synthetic data (mixture of gaussians around random centroids) and re-measure.
"""
import numpy as np, time
NDIM = 1024
rng = np.random.default_rng(11)
def norm(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True); n[n==0]=1.0
    return (v/n).astype(np.float32)

N = 30000
NQ = 200
K = 10
NCLUST = 200
centroids = norm(rng.standard_normal((NCLUST, NDIM)).astype(np.float32))
assign = rng.integers(0, NCLUST, N)
db = norm(centroids[assign] + 0.35 * rng.standard_normal((N, NDIM)).astype(np.float32))
# queries near random centroids too
qassign = rng.integers(0, NCLUST, NQ)
qs = norm(centroids[qassign] + 0.35 * rng.standard_normal((NQ, NDIM)).astype(np.float32))

print("computing exact ground truth (clustered) ...")
gt = []
for q in qs:
    s = db @ q
    top = np.argpartition(-s, K)[:K]; top = top[np.argsort(-s[top])]
    gt.append(set(top.tolist()))

def rec(preds, k=K):
    return np.mean([len(set(p[:k]) & g)/k for p,g in zip(preds, gt)])

# int8 brute (sqlite-vec equivalent)
def q8(v): return np.clip(np.round(v*127),-127,127).astype(np.int8)
db8, qs8 = q8(db), q8(qs)
pred_i8=[];
for q in qs8:
    sc = db8.astype(np.int32) @ q.astype(np.int32)
    pred_i8.append(np.argsort(-sc)[:3*K].tolist())
print(f"int8 brute recall@{K}(top-k) = {rec([p[:K] for p in pred_i8]):.4f}")
pred2=[]
for i,c in enumerate(pred_i8):
    c=np.array(c); s=db[c]@qs[i]; pred2.append(c[np.argsort(-s)][:K].tolist())
print(f"int8 coarse 3k -> fp32 rerank recall@{K} = {rec(pred2):.4f}")

from usearch.index import Index
keys=np.arange(N,dtype=np.uint64)
for dt in ['f32','bf16','i8']:
    idx=Index(ndim=NDIM,metric='cos',dtype=dt)
    t=time.perf_counter(); idx.add(keys,db,threads=0); bt=time.perf_counter()-t
    t=time.perf_counter(); preds=[]
    for q in qs:
        preds.append([int(x) for x in idx.search(q,K).keys])
    qt=(time.perf_counter()-t)/NQ*1000
    print(f"\nusearch {dt}: build {bt:.2f}s, {qt:.2f} ms/q, recall@{K} = {rec(preds):.4f}")
    # ANN coarse 3k -> fp32 rerank
    pr=[]
    for i,q in enumerate(qs):
        c=np.array([int(x) for x in idx.search(q,3*K).keys])
        s=db[c]@q; pr.append(c[np.argsort(-s)][:K].tolist())
    print(f"  {dt} coarse 3k -> fp32 rerank recall@{K} = {rec(pr):.4f}")
