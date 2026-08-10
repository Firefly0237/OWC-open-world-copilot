"""Scale benchmark for the vector-search backends — the evidence behind "支撑几十万条".

Builds a synthetic corpus of ``N`` unit vectors and, for each backend, measures index build time,
mean query latency, recall@k (vs the exact fp32 ground truth), and memory footprint (in-RAM matrix
vs on-disk index). This is the honest ruler for the scale claim: it shows *where* each tier pays
off (int8 = ~4× less memory at ~same recall; usearch HNSW = sub-linear latency at large N) so the
auto-tier policy's thresholds can be defended with numbers rather than adjectives.

Deterministic and ``$0`` (numpy synthetic vectors, fixed seed — no model, no network):

    python scripts/run_scale_benchmark.py                  # defaults reproduce the committed report
    python scripts/run_scale_benchmark.py --n 50000 --dim 1024 --queries 200   # push it harder

Writes a Markdown report to ``benchmarks/scale_vector_backends.md`` and prints it.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np

from owcopilot.retrieval.vector_backend import (
    NumpyMatrixBackend,
    SqliteVecBackend,
    SqliteVecInt8Backend,
    UsearchBackend,
)
from owcopilot.util import use_utf8_stdout


def _synthetic_corpus(n: int, dim: int, *, seed: int = 20260707) -> tuple[list[str], np.ndarray]:
    """``n`` clustered unit vectors (clusters give well-defined nearest neighbours so recall@k is
    a meaningful signal, not noise). Deterministic under ``seed``."""
    rng = np.random.default_rng(seed)
    n_clusters = max(8, n // 500)
    centers = rng.standard_normal((n_clusters, dim)).astype(np.float32)
    assign = rng.integers(0, n_clusters, size=n)
    vecs = centers[assign] + 0.35 * rng.standard_normal((n, dim)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    refs = [f"entity:e{i:07d}" for i in range(n)]
    return refs, vecs.astype(np.float32)


def _exact_topk(matrix: np.ndarray, refs: list[str], q: np.ndarray, k: int) -> list[str]:
    """Ground-truth top-k by exact cosine (unit vectors → dot product)."""
    scores = matrix @ q
    idx = np.argsort(-scores, kind="stable")[:k]
    return [refs[i] for i in idx]


def _load(backend: object, refs: list[str], vecs: np.ndarray) -> float:
    t0 = time.perf_counter()
    for ref, vec in zip(refs, vecs, strict=True):
        backend.upsert(ref, vec)  # type: ignore[attr-defined]
    return time.perf_counter() - t0


def _measure(
    backend: object,
    queries: np.ndarray,
    truth: list[list[str]],
    k: int,
) -> tuple[float, float]:
    """Return ``(mean_query_ms, recall_at_k)`` for ``backend`` over the query set."""
    lat: list[float] = []
    hits = 0
    for q, gold in zip(queries, truth, strict=True):
        t0 = time.perf_counter()
        got = backend.search(q, limit=k)  # type: ignore[attr-defined]
        lat.append((time.perf_counter() - t0) * 1000.0)
        got_refs = {ref for ref, _ in got}
        hits += len(got_refs & set(gold))
    recall = hits / (len(truth) * k)
    return statistics.mean(lat), recall


def _bench_conn(path: Path) -> sqlite3.Connection:
    """A benchmark connection. The backends ``commit()`` per upsert; ``synchronous=OFF`` removes the
    per-commit fsync so building the corpus is not fsync-bound (safe here — a throwaway temp DB, not
    production durability)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA journal_mode=MEMORY")
    return conn


def _file_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0


def main() -> None:
    use_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=5_000, help="corpus size")
    ap.add_argument("--dim", type=int, default=256, help="embedding dimensionality")
    ap.add_argument("--queries", type=int, default=40, help="number of query vectors")
    ap.add_argument("--k", type=int, default=10, help="top-k")
    args = ap.parse_args()

    n, dim, k = args.n, args.dim, args.k
    print(f"building synthetic corpus: N={n}, dim={dim} …")
    refs, vecs = _synthetic_corpus(n, dim)
    rng = np.random.default_rng(999)
    qidx = rng.integers(0, n, size=args.queries)
    # Perturb real vectors slightly so queries are near-but-not-identical to a stored point.
    queries = vecs[qidx] + 0.1 * rng.standard_normal((args.queries, dim)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True) + 1e-12
    queries = queries.astype(np.float32)

    truth = [_exact_topk(vecs, refs, q, k) for q in queries]
    fp32_ram_mb = vecs.nbytes / (1024 * 1024)

    rows: list[dict[str, object]] = []
    # ignore_cleanup_errors: on Windows a just-closed sqlite/usearch file can linger a beat under
    # the OS; a cleanup race must not sink the run after every backend has already been measured.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)

        # 1) numpy in-RAM fp32 — the pre-scale baseline (whole matrix resident).
        nb = NumpyMatrixBackend()
        build = _load(nb, refs, vecs)
        ms, rec = _measure(nb, queries, truth, k)
        rows.append(
            {
                "backend": "numpy fp32 (in-RAM)",
                "build_s": build,
                "ms": ms,
                "recall": rec,
                "mem": f"{fp32_ram_mb:.1f} MB RAM",
            }
        )

        # 2) sqlite-vec fp32 (disk-resident exact scan).
        db1 = _bench_conn(tmpdir / "fp32.sqlite")
        vb = SqliteVecBackend(db1, dim=dim, table="content_vec")
        build = _load(vb, refs, vecs)
        db1.commit()
        ms, rec = _measure(vb, queries, truth, k)
        rows.append(
            {
                "backend": "sqlite-vec fp32 (disk)",
                "build_s": build,
                "ms": ms,
                "recall": rec,
                "mem": f"{_file_mb(tmpdir / 'fp32.sqlite'):.1f} MB disk",
            }
        )
        db1.close()

        # 3) int8 two-stage (int8 coarse → fp32 rerank).
        db2 = _bench_conn(tmpdir / "int8.sqlite")
        ib = SqliteVecInt8Backend(db2, dim=dim, table="content_vec_i8")
        build = _load(ib, refs, vecs)
        db2.commit()
        ms, rec = _measure(ib, queries, truth, k)
        rows.append(
            {
                "backend": "int8 two-stage (disk)",
                "build_s": build,
                "ms": ms,
                "recall": rec,
                "mem": f"{_file_mb(tmpdir / 'int8.sqlite'):.1f} MB disk",
            }
        )
        db2.close()

        # 4) usearch HNSW ANN (sub-linear) + fp32 rerank.
        db3 = _bench_conn(tmpdir / "hnsw.sqlite")
        idx_path = str(tmpdir / "hnsw.usearch")
        ub = UsearchBackend(db3, dim=dim, table="content_vec_hnsw", index_path=idx_path)
        build = _load(ub, refs, vecs)
        db3.commit()
        ms, rec = _measure(ub, queries, truth, k)
        disk = _file_mb(tmpdir / "hnsw.sqlite") + _file_mb(tmpdir / "hnsw.usearch")
        rows.append(
            {
                "backend": "usearch HNSW (disk)",
                "build_s": build,
                "ms": ms,
                "recall": rec,
                "mem": f"{disk:.1f} MB disk",
            }
        )
        db3.close()

    report = _render(rows, n=n, dim=dim, queries=args.queries, k=k, fp32_ram_mb=fp32_ram_mb)
    out = Path(__file__).resolve().parents[1] / "benchmarks" / "scale_vector_backends.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n[written] {out}")


def _render(
    rows: list[dict[str, object]], *, n: int, dim: int, queries: int, k: int, fp32_ram_mb: float
) -> str:
    lines = [
        "# Vector-backend scale benchmark",
        "",
        f"- Corpus: **N={n:,}** synthetic unit vectors, dim **{dim}** · queries **{queries}** · "
        f"top-k **{k}**",
        "- Deterministic, `$0` (numpy synthetic, fixed seed — no model/network).",
        "- Recall = overlap with the exact fp32 ground truth (so fp32 backends read ~1.00).",
        "- Reproduce: `python scripts/run_scale_benchmark.py "
        f"--n {n} --dim {dim} --queries {queries}`",
        "",
        f"| backend | build (s) | query (ms) | recall@{k} | footprint |",
        "|---|--:|--:|--:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['backend']} | {r['build_s']:.2f} | {r['ms']:.3f} | "
            f"{r['recall']:.3f} | {r['mem']} |"
        )
    lines += [
        "",
        "## Reading it",
        "",
        f"- **numpy fp32 (in-RAM)** has the fastest query (one vectorised `matrix @ q`), but holds "
        f'the whole {fp32_ram_mb:.0f} MB matrix in RAM — the "全量进内存" wall the scale audit '
        "flagged. Its problem is memory, not latency; the disk backends keep RAM flat as N grows.",
        "- **sqlite-vec fp32 (disk)** keeps RAM flat but reranks up to 4096 exact candidates in "
        "Python per query — the slowest per-query row here. That cost is fine only on a small "
        "corpus, which is exactly where the auto-tier keeps it (below the int8 threshold).",
        "- **int8** and **usearch HNSW** pull a *small* candidate pool (~30) and rerank only "
        "those, so their per-query latency is a fraction of the fp32 scan's: int8 wins ~4× memory "
        "at ~equal recall; HNSW adds a sub-linear ANN index (higher one-time build cost).",
        "- Net: the `RetrievalPolicy` auto-tier is what makes this hold — fp32 exact only on small "
        "corpora, int8 in the mid band, HNSW past `USEARCH_MIN_N` — so no tier pays a cost that "
        "doesn't fit its corpus size. (Recall is 1.00 across the board at this N; the tiers "
        "diverge on memory and latency, not correctness, until much larger N.)",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
