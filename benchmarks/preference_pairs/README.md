# 偏好数据导出样例 — OWCopilot 数据闭环

`complete_pairs.jsonl` 是 OWCopilot **真实 critique→refine 循环**（真实 DeepSeek 生成 + 真实
critic 修订）产出的完整偏好对数据。每行顶层 `prompt` / `chosen` / `rejected` 均为字符串
（chosen/rejected 是 JSON 编码的任务草稿），完整溯源在 `meta`；格式可交给下游
DPO-style trainer 或偏好评测脚本消费。

- **语义**：改进对（improvement pair）——同一任务下，修订后草稿优于修订前快照；偏好由
  **critic 分数上升**锚定（`meta.rejected_score → meta.chosen_score`），或最终稿过审锚定。
- **当前规模**：N=3（开发规模示例）。管线随创作工作流持续供给——每次带修订轮的生成都可能
  产出新对（`scripts/collect_preference_pairs.py` 可复跑，约 $0.05/轮）。
- **正确性保证**：提取逻辑由单元测试覆盖（`tests/test_dpo_export.py`），含快照落袋、
  分数上升锚定、未过审终稿不出对、DPO 格式兼容。
- **诚实注记**：N=3 只是开发规模样例，不足以支撑有统计意义的训练结论。这里展示的是
  **偏好数据导出/交接管线已跑通**，不是“模型已微调”、不是效果宣称，也没有产出新权重。

下游实验消费方式：
```python
from datasets import load_dataset
ds = load_dataset("json", data_files="complete_pairs.jsonl")["train"]
# 列: prompt / chosen / rejected（str）→ 可交给 DPO-style trainer/评测脚本
```
