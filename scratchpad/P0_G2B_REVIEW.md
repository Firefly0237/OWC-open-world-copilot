# G2-B 复查（编排者执行——复查 agent 连续随进程退出，编排者接手 + 全测试套件作独立验证）

裁决：**PASS**。背景：G2-B 执行 agent 进程退出未报告但代码完整；编排者补做 ruff/mypy 清理（usearch Index `object`→`Any`、6 E501 换行、测试 `"r%d"%i`→f-string，纯清理不改行为）；复查 agent 亦随进程退出。编排者以「代码审读 backstop + 全量测试套件（每个关键关注点都有专门测试且全绿）」作独立验证。

逐项（证据=代码 file:line + 对应 passing 测试，全量 1492 passed 内）：
1. **真 usearch 非搭壳**：usearch 2.25.3 真装项目 venv；`UsearchBackend` 真建 HNSW（vector_backend.py `_new_index` 用固定常量 `_USEARCH_CONNECTIVITY=32/_EXPANSION_ADD=200/_EXPANSION_SEARCH=2048`，非 library 默认，451-460 有实测依据注释——执行 agent 还把 expansion_search 从研究的 512 提到 2048 以稳过 0.95 召回门）。测试 test_usearch_upsert_search_vector_for_and_delete / on_disk_save_and_reopen_roundtrip（mmap）。
2. **召回**：test_usearch_tuned_two_stage_recall_is_high_and_beats_default_params（调参+两阶段 ≥0.95 且 > 默认参）。vector_for 返回精确 fp32（两阶段精排源=fp32 sidecar）。
3. **ref↔uint64 key**：test_usearch_string_ref_key_mapping_roundtrips（63-bit hash + linear probe + keymap 表，跨重启稳定）。
4. **一致性/可重建**（关键，SQLite 事务外）：test_usearch_dirty_index_rebuilds_from_fp32_source + test_usearch_corrupt_index_file_rebuilds（count/损坏不符 → 从权威 fp32 源 self-heal 重建）。
5. **分层选择**：make_vector_backend(ann=True) 仅 corpus ≥ 阈值用 usearch；test_make_vector_backend_small_n_stays_on_sqlite_vec_even_with_ann + large_n_with_ann_uses_usearch；**eval-acceptance 两个 retrieval 召回门实测仍 1.0**（小 N 走 sqlite-vec，ANN 近似不影响 eval）。
6. **不静默降级**：test_make_vector_backend_falls_back_to_sqlite_vec_when_usearch_unavailable（import 失败 guided 回退）。
7. **无回归无越界**：全量 1492 passed/2 skipped、ruff/mypy(231) 绿、eval-acceptance+golden 召回门 1.0；diff 仅 vector_backend/vector/sqlite/tests/pyproject；无 world_id/partition/shard（未越 G2-C 界）；编排者清理（object→Any 等）经全套验证无行为变化。

一句话：PASS——usearch ANN tier 真实落地（真接、调参有据、两阶段召回≥0.95、ref↔key/self-heal/分层/guided 回退齐备），eval 召回门因小 N 留在 sqlite-vec 仍 1.0，全门禁绿、零回归零越界。
