# Retrieval and chunking benchmarks

本目录保存检索与分块的冻结回归数据及报告：

- `datasets/retrieval-qrels-v1.json`：BM25、Dense、RRF 与 Rerank 的查询相关性标注；
- `reports/retrieval-report-v1.json`：bge-m3 与 bge-reranker-v2-m3 的 dev 选参与 held-out 结果；
- `../retrieval_eval_set_v1.json`：120 条冻结的 ContextPack production-path 诊断查询；
- `reports/retrieval-diagnostics-v1.json`：`$0` recall、FP2/FP3 与配对 McNemar A/B 机制报告；
- `datasets/chunking-cases-v1.json`：结构/语义边界和意图连贯性用例；
- `reports/chunking-report-v1.json`：结构策略与语义策略的边界保持结果。

重跑入口：

```powershell
python scripts/eval_retrieval_benchmark.py
python scripts/run_frozen_retrieval_diagnostics.py
python scripts/eval_chunking_benchmark.py
```

真实模型评测只使用 dev split 选择参数，并在 held-out split 上报告最终指标。冻结诊断显式使用
`HashingEmbedder`，只验证 production ContextPack 机制、FP2/FP3 故障归因和配对检验流程；其绝对
recall/hit rate 不代表语义 embedding 或神经 reranker 质量。
