"""Probe 3c: ANN recall vs data tightness at 1024-dim, + does coarse->rerank recover it?

Sweep cluster spread sigma. Smaller sigma = tighter clusters = more separable neighbor
structure (what real embeddings have). For each: usearch f32 ANN recall@10, and
ANN-coarse-k'=5k -> fp32 rerank recall@10. Also high-expansion ANN.
"""
import numpy as np, time
from usearch.index import Index
NDIM=1024; rng=np.random.default_rng(9)
def norm(v):
    n=np.linalg.norm(v,axis=-1,keepdims=True); n[n==0]=1.0; return (v/n).astype(np.float32)
N=30000; NQ=100; K=10
NCLUST=200
cent=norm(rng.standard_normal((NCLUST,NDIM)).astype(np.float32))
asg=rng.integers(0,NCLUST,N)
keys=np.arange(N,dtype=np.uint64)
for sigma in [0.05,0.1,0.2,0.35,0.6]:
    db=norm(cent[asg]+sigma*rng.standard_normal((N,NDIM)).astype(np.float32))
    qa=rng.integers(0,NCLUST,NQ)
    qs=norm(cent[qa]+sigma*rng.standard_normal((NQ,NDIM)).astype(np.float32))
    gt=[]
    for q in qs:
        s=db@q; t=np.argpartition(-s,K)[:K]; gt.append(set(t.tolist()))
    def rec(preds): return np.mean([len(set(p)&g)/K for p,g in zip(preds,gt)])
    idx=Index(ndim=NDIM,metric='cos',dtype='f32',expansion_add=128,expansion_search=64)
    idx.add(keys,db,threads=0)
    ann=[[int(x) for x in idx.search(q,K).keys] for q in qs]
    # coarse 5k -> fp32 rerank
    rr=[]
    for i,q in enumerate(qs):
        c=np.array([int(x) for x in idx.search(q,5*K).keys]); s=db[c]@q
        rr.append(c[np.argsort(-s)][:K].tolist())
    # high expansion ANN
    idx2=Index(ndim=NDIM,metric='cos',dtype='f32',connectivity=32,expansion_add=256,expansion_search=256)
    idx2.add(keys,db,threads=0)
    ann2=[[int(x) for x in idx2.search(q,K).keys] for q in qs]
    print(f"sigma={sigma:<4} ANN recall@10={rec(ann):.3f}  ANN(hi-exp)={rec(ann2):.3f}  ANN5k->rerank={rec(rr):.3f}")
