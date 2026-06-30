"""Is usearch's 0.12 recall a param bug? Test expansion_search + connectivity."""
import numpy as np, time
from usearch.index import Index
NDIM=1024; N=30000; NQ=200; K=10
rng=np.random.default_rng(7)
def norm(v):
    n=np.linalg.norm(v,axis=-1,keepdims=True); n[n==0]=1; return (v/n).astype(np.float32)
# clustered
C=200; cent=norm(rng.standard_normal((C,NDIM)))
lbl=rng.integers(0,C,N); data=norm(cent[lbl]+0.35*rng.standard_normal((N,NDIM)))
q=data[rng.choice(N,NQ,replace=False)]
# exact GT (cosine == dot on normalized)
gt=np.argsort(-(q@data.T),axis=1)[:,:K]
def recall(pred):
    return np.mean([len(set(pred[i])&set(gt[i]))/K for i in range(NQ)])
keys=np.arange(N,dtype=np.uint64)
for conn,exp_add,exp_s in [(16,128,64),(16,128,256),(32,200,512)]:
    idx=Index(ndim=NDIM,metric='cos',dtype='f32',connectivity=conn,expansion_add=exp_add,expansion_search=exp_s)
    t=time.perf_counter(); idx.add(keys,data); bt=time.perf_counter()-t
    t=time.perf_counter()
    m=idx.search(q,K)
    qt=(time.perf_counter()-t)/NQ*1000
    pred=m.keys  # (NQ,K)
    r=recall(pred)
    print(f"conn={conn} exp_add={exp_add} exp_search={exp_s}: build {bt:.1f}s, {qt:.2f}ms/q, recall@{K}={r:.4f}")
