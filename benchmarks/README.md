# Benchmarks

本目录保存可复算的回归数据、评测定义与系统运行结果：

- [`evaluation/`](evaluation/)：检索与分块的冻结数据集、选参方法和 held-out 报告；
- [`formal/`](formal/)：版本化 Suite、Contract、Control、Fault 与 Artifact 校验策略；
- [`evidence/system-demo/`](evidence/system-demo/)：原生工具与 MCP 路径的系统冒烟结果；
- [`evidence/narrative-complexity/`](evidence/narrative-complexity/)：叙事复杂度画像与来源分析；
- [`preference_pairs/`](preference_pairs/)：供下游偏好优化流程使用的改进对样例。

新运行产物默认写入 `.tmp/`。需要长期保留的结果应与数据集、配置和可复算入口一同版本化。