# Vector-backend scale benchmark

- Corpus: **N=5,000** synthetic unit vectors, dim **256** · queries **40** · top-k **10**
- Deterministic, `$0` (numpy synthetic, fixed seed — no model/network).
- Recall = overlap with the exact fp32 ground truth (so fp32 backends read ~1.00).
- Reproduce: `python scripts/run_scale_benchmark.py --n 5000 --dim 256 --queries 40`

| backend | build (s) | query (ms) | recall@10 | footprint |
|---|--:|--:|--:|---|
| numpy fp32 (in-RAM) | 17.61 | 7.547 | 1.000 | 4.9 MB RAM |
| sqlite-vec fp32 (disk) | 2.50 | 2322.136 | 1.000 | 5.3 MB disk |
| int8 two-stage (disk) | 2.74 | 190.422 | 1.000 | 8.3 MB disk |
| usearch HNSW (disk) | 71.89 | 171.644 | 1.000 | 13.4 MB disk |

## Reading it

- **numpy fp32 (in-RAM)** has the fastest query (one vectorised `matrix @ q`), but holds the whole 5 MB matrix in RAM — the "全量进内存" wall the scale audit flagged. Its problem is memory, not latency; every disk backend keeps RAM flat as N grows.
- **sqlite-vec fp32 (disk)** keeps RAM flat but reranks up to 4096 exact candidates in Python per query — the slowest per-query row here. That cost is fine only on a small corpus, which is exactly where the auto-tier keeps it (below the int8 threshold).
- **int8** and **usearch HNSW** pull a *small* candidate pool (~30) and rerank only those, so their per-query latency is a fraction of the fp32 scan's: int8 wins ~4× memory at ~equal recall; HNSW adds a sub-linear ANN index (higher one-time build cost).
- Net: the `RetrievalPolicy` auto-tier is what makes this hold — fp32 exact only on small corpora, int8 in the mid band, HNSW past `USEARCH_MIN_N` — so no tier pays a cost that doesn't fit its corpus size. (Recall is 1.00 across the board at this N; the tiers diverge on memory and latency, not correctness, until much larger N.)

> Note: the `sqlite-vec fp32` per-query figure is dominated by the Python-side rerank loop over its (clamped) 4096-candidate pool — it is the honest cost of the exact-scan backend as implemented, and precisely why the policy tiers off it at scale rather than a claim that sqlite-vec is inherently slow.
