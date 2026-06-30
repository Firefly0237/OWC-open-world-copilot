# G2-C 设计：world_id/version 分片（架构级，编排者起草）

## 现状（约束设计）
- `content/models.py`：`Entity.version` 是自由文本（非分区维度）；无 `world_id`；`Relation` 有 `valid_from/valid_until`（时间窗）。
- `ContentBundle` = 单一扁平世界，全量进内存 dict。
- snapshot = 整 bundle 时间点全量拷贝（content/snapshot.py）；不是增量版本维度。
- 一个 `content_root` = 一个世界；SQLite 表（content_index/graph_edges/reference/vec0）无 scope 列。
- sqlite-vec PARTITION KEY 已验证可用（可作 vec0 的分片维度）。

## 分片要交付的核心价值（来自 SCALE_AUDIT #0）
把每查询/每审计的 N 从「全历史几十万」降到「当前 scope 几千~几万」——让暴力/int8 也够快，连带缓解审计/社区/impact 的全量扫描。**关键不是"多世界管理"，是"检索/审计默认只在当前 scope 跑"。**

## 两个 scope 维度（候选）
- **world_id**：多世界隔离（一个工作台管多个游戏/项目）。
- **version**：同一世界的版本线（5 年近百版本）。检索通常要「当前版本 + 其继承的基线」。
- 二者可正交（world_id × version）。

## 设计决策的真正分叉（需用户拍板）
**A. 最小可用「scope 维度」基础（推荐）**：给内容模型 + SQLite 表加一个 scope 维度（建议 `scope` 复合键，承载 world_id/version），检索/审计/impact 默认 filter 到「当前 scope」；vec0 用 PARTITION KEY。**交付"降N"价值**，不做多世界管理 UI、不做版本继承/合并语义、不做冷热归档——那些明确划 P1。改动可控、可分小单元提交。
**B. 完整多世界/版本数据模型**：models 加 world_id+version、版本继承（当前版本看不到的回退到基线）、跨版本 diff/合并、冷热分层归档、多世界切换 UI/API。**最全但最大**，是多日架构工程，且与现 snapshot 机制要统一（snapshot vs version 的关系要重新设计），destabilize 面广。

## 推荐：A（scope 维度基础），分 3 个可提交小单元
- **C1（数据模型 + schema）**：ProvenanceMixin/或 ContentBundle 加 `scope`（world_id+version 复合，默认单一 "default" scope 向后兼容）；SQLite content_index/graph_edges/reference/content_vectors/vec0 加 scope 列 + 索引 + vec0 PARTITION KEY；迁移：旧数据归入 "default" scope（零行为变化）。
- **C2（检索/审计默认 scope）**：retrieval（bm25/vector/graph/context_pack）+ audit + impact 默认只在「当前 scope」跑；ProjectContext 携带当前 scope；未指定=default（向后兼容，eval 世界=单 scope，门不变）。
- **C3（version 继承语义，可选/可缩）**：「当前版本 + 继承基线」的读取语义（当前 scope 查不到的 fork 到基线）。若太大可缩为「版本=独立 scope，无继承」并把继承留 P1。
- 每单元执行→复查→门禁绿；C 全部完后组 2 验收+监督。
- 红线同前：真实落地、向后兼容（default scope 单世界行为逐字节不变、eval 门 1.0）、不静默降级、$0、分小单元各自可提交绿。

## 用户已拍板（2026-06-30）：**B 完整多世界/版本，world_id + version 都上**

### B 的分解（6 个可单独提交的小单元，各 执行→复查→门禁绿；限制进程崩溃的爆炸半径）
- **C1 scope 维度基础（数据模型 + schema + 迁移）**：ProvenanceMixin 加 `world_id: str="default"`、`version: str="v1"`（默认值=向后兼容，现有单世界行为逐字节不变）；SQLite content_index/graph_edges/reference_sources/reference_chunks/content_vectors/reference_vectors 加 `world_id`+`version` 列 + 复合索引；vec0 表用这两列作 PARTITION KEY；迁移：现有行归 ("default","v1")。ProjectContext 携带当前 (world_id, version)，默认 ("default","v1")。eval 世界=单 scope，门不变。
- **C2 检索/审计/impact 默认 scope**：retrieval(bm25/vector/graph/context_pack)+audit+impact 默认只 filter 当前 scope；未指定=default（向后兼容）。这是"降N"价值兑现处。
- **C3 version 继承语义**：「当前版本 + 继承基线」读取——当前 version 查不到的对象 fork 到其 base version；版本创建=从 base 分支（copy-on-write，不复制全量）。
- **C4 跨版本 diff/merge + 与 snapshot 统一**：重新厘清 snapshot（时间点全量拷贝）vs version（分片维度）的关系，统一为一套；版本 diff 复用 bundle_diff；merge。
- **C5 冷热分层归档**：历史版本归档/按需 lazy-load，只有 active version 的索引常驻；历史检索按需挂载分片。
- **C6 多世界切换**：world 管理（create/list/switch）+ CLI/API。
- 全部完后：组 2 整体验收 + 监督，再总结归纳收口整个 P0 程序。

### 关键设计决策（执行须遵）
- scope 复合键 = (world_id, version)；默认 ("default","v1") 保证向后兼容、eval 门不变。
- 内容文件仍可按 content_root 组织；runtime SQLite 按 scope 列分片（单库多 scope）。vec0 PARTITION KEY 已验证支持。
- 红线同前：真实落地、向后兼容（default scope 单世界逐字节不变、eval 1.0）、不静默降级、$0、每单元可单独提交绿。
- C3/C4 语义较深，执行前各自可再细化设计；C5/C6 偏工程。
