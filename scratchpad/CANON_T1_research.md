# Team 1 调研产出 — P0 路径穿越统一 + P3-b 注入防御表述

分支 feature/agent-pipeline-enhancements @ 7e8adc0。本文件**只是方案**，不改任何代码。

---

## 现状速读（关键事实）

### `trust/security.py` 已有的 canon 容器检查
```python
def resolve_under_root(root: str | Path, candidate: str | Path) -> Path:
    resolved_root = Path(root).expanduser().resolve()
    candidate_path = Path(candidate).expanduser()
    resolved = (
        candidate_path.resolve()
        if candidate_path.is_absolute()
        else (resolved_root / candidate_path).resolve()
    )
    try:
        resolved.relative_to(resolved_root)
    except ValueError as e:
        raise PathSecurityError(f"path {resolved} escapes allowed root {resolved_root}") from e
    return resolved
```
- `PathSecurityError(ValueError)`（`trust/security.py:8`），所以 `except ValueError` 仍能兜住，CLI 全局错误边界能格式化。
- 已导出：`from owcopilot.trust import PathSecurityError, resolve_under_root`（`trust/__init__.py:10`）。
- 已在用：`service/api.py:3257` 导出目录拼接用它；zip 导入（`app/workspaces.py:138-148`）用的是**手写的同款 resolve+parents 检查**（不是直接调 `resolve_under_root`，但同思路）。

### 现状的「字符黑名单」第一层
- `content/normalize.py:31` `_FORBIDDEN_ID_CHARS = frozenset("/\\.:")`；`_validate_id_chars(value, *, context, forbidden)`（:39）校验 空白/控制字符/禁用字符/`..`/长度，抛 domain `ValueError`。
- store 已在写边界用它：`content/store.py:142-147`（`_write_json_dir` 对每个 object_id 校验）。
- snapshot 已在读边界用它：`content/snapshot.py:106-108`（`load_snapshot` 对 `snapshot_id` 校验）。

> 即：第一层（字符黑名单）**已经覆盖了所有 `{id}.json` 路径**。本任务的增量 = 在**最终文件路径**处再叠加 `resolve_under_root` 容器断言，形成「友好字符层 + canon 容器层」的双层、统一防线，并消除「store/snapshot 用黑名单、workspaces 用手写 resolve」的两套风格。

---

## P0 调研结论

### content_root 怎么取
- **store**：`ContentStore.__init__` 存 `self.root = Path(root)`（`store.py:35-36`）。容器根 = `self.root`。所有 per-file 写法都是 `self.root / <subdir> / f"{id}.json"`。
- **snapshot**：函数签名 `write_snapshot(store, ...)` / `load_snapshot(store, snapshot_id)`，容器根 = `store.root / ".snapshots"`（`_SNAP_DIR = ".snapshots"`，snapshot.py:23）。最终文件 = `store.root / _SNAP_DIR / f"{id}.json"`。
- **inspiration**（领地内，附带核查）：`ReferenceStore.root = Path(content_root) / "references"`，写 `sources/{id}.json`、`raw/{id}.txt`（`inspiration/store.py:23-26, 59, 68`）。其 id 由 `_unique_source_id` 内部合成（`ref_<slug>_<hash8>`，:161-169），非外部直控；优先级低，建议本轮可一并加但不强求（见下）。

### 哪些路径是 `{id}.json`（需校验）、哪些走 jsonl/aggregate（别误拦）
**需加容器断言（per-file `{id}.json`，id 进文件名）：**
| 位置 | file:line | 最终路径表达式 | 根 |
|---|---|---|---|
| store `_write_json_dir` 写循环 | `store.py:153-154` | `path / f"{object_id}.json"` | `self.root` |
| store `_load_json_dir` 读循环 | `store.py:82-83` | `file_path`（来自 `path.glob("*.json")`） | `self.root` |
| snapshot `write_snapshot` | `snapshot.py:80` | `store.root / _SNAP_DIR / f"{snap_id}.json"` | `store.root` |
| snapshot `load_snapshot` | `snapshot.py:109` | `store.root / _SNAP_DIR / f"{safe_id}.json"` | `store.root` |
| snapshot `list_snapshots` 读循环 | `snapshot.py:92` | `path`（来自 `directory.glob("*.json")`） | `store.root` |

**不要碰（不经 `{id}.json`，走聚合/jsonl/固定文件名，id 只作 JSON 内容不进文件名）：**
- `_write_relations` → `world/relations.jsonl`（固定名，store.py:156-160）
- `_write_quest_event_refs` → `quests/event_refs.jsonl`（固定名，store.py:162-166）— **qer 合成冒号 id `quest:event:kind` 在此，不进文件名，不会被拦**。
- `_write_terms` → `world/terms.json`（固定名，store.py:168-172）
- `_write_style_guides` → `world/style_guides.json`（固定名，store.py:174-189）
- 对应读取器 `_load_relations`/`_load_quest_event_refs`/`_load_terms`/`_load_style_guides`（固定名）。

### 误拦风险分析（逐一确认安全）
1. **时间戳 snapshot id**（`write_snapshot`，snapshot.py:72：`%Y%m%d_%H%M%S_%f`，形如 `20260629_143022_123456`）：只含数字+下划线，无 `/ \ . : ..`，`resolve_under_root` 拼成 `<root>/.snapshots/20260629_..._123456.json`，`relative_to(root)` 必过。**不会误拦。** ✔
2. **qer 合成冒号 id** `quest:event:kind`：只出现在 `event_refs.jsonl`（聚合），**根本不经任何 `{id}.json` 路径**，本轮插入点都碰不到它。即便假设性地经过 resolve（不会发生），冒号在 POSIX 不是分隔符、在 Windows 仅当紧跟盘符字母才有意义（`quest:` 不是 `C:`），`(root / "quest:event:kind").resolve()` 仍在 root 下 — 但这是纯理论，实际不触发。**不会误拦。** ✔
3. **正常 slug id**（`slug_id` 产出 `[a-z0-9_]`，normalize.py:139-147）：纯安全字符。✔
4. **localized_text id** `slug_id(text_id, prefix="loc")`（normalize.py:508）：已 slug 化，安全。✔
5. **绝对路径 / 盘符**：`resolve_under_root` 对绝对 candidate 也做 `relative_to` 检查，逃逸即抛 — 这正是我们要的第二层。但注意：插入点传入的都是**已经拼好的 `path / f"{id}.json"`**（绝对路径，且本就在 root 下），所以 candidate 走 `is_absolute()` 分支、resolve 后 relative_to 通过。✔

> 结论：所有合法 id 都不会被 `resolve_under_root` 误拦；非法 id（`../`、`..\`、盘符、嵌套分隔符）在第一层 `_validate_id_chars` 已被拒（它禁 `/ \ . :` + `..` + 控制字符），第二层 `resolve_under_root` 是 defense-in-depth 的容器兜底。

---

## P0 确切插入点设计（file:line + 函数签名）

复用：`from ..trust.security import resolve_under_root`（snapshot.py / store.py 顶部加导入；`PathSecurityError` 是 `ValueError` 子类，CLI 边界已能接，无需额外 except）。

> 设计取舍：第一层 `_validate_id_chars` **保留不动**（友好报错、命名禁用字符 + context；现有测试 `test_snapshot_diff.py:105-130` 依赖 `"context"`/`"snapshot_id"` 在消息里）。第二层只在**最终路径**叠加，二者职责清晰、不冲突。

### 插入点 1 — `store.py::_write_json_dir`（写边界，最高价值）
当前（store.py:148-154）：
```python
        path.mkdir(parents=True, exist_ok=True)
        ...
        for object_id, model in sorted(objects.items()):
            self._write_json(path / f"{object_id}.json", model)
```
方案：在写每个文件前，对**最终路径**做容器断言（root = `self.root`）：
```python
        for object_id, model in sorted(objects.items()):
            target = path / f"{object_id}.json"
            resolve_under_root(self.root, target)   # canon container assertion (2nd layer)
            self._write_json(target, model)
```
- 注：循环顶部（:142-147）的 `_validate_id_chars` 已先跑、已拒绝坏 id，这里是兜底。两层并存，注释里点明分工即可（沿用 store.py:133-141 现有大段注释风格）。

### 插入点 2 — `snapshot.py::write_snapshot`（写边界）
当前（snapshot.py:80-82）：
```python
    path = store.root / _SNAP_DIR / f"{snap_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(...)
```
方案（root = `store.root`，因 `.snapshots` 是 root 的合法子目录，断言到 root 即可，亦可断言到 `store.root / _SNAP_DIR`，二者都对；建议用 `store.root` 与 load 对齐）：
```python
    path = store.root / _SNAP_DIR / f"{snap_id}.json"
    resolve_under_root(store.root, path)   # snap_id is internal timestamp; assert anyway
    path.parent.mkdir(parents=True, exist_ok=True)
```

### 插入点 3 — `snapshot.py::load_snapshot`（读边界）
当前（snapshot.py:106-109）：
```python
    safe_id = _validate_id_chars(snapshot_id, context="snapshot_id (load_snapshot)", forbidden=_FORBIDDEN_ID_CHARS)
    path = store.root / _SNAP_DIR / f"{safe_id}.json"
    if not path.exists():
```
方案：第一层 `_validate_id_chars` 保留（友好 + 现有测试依赖），第二层叠加：
```python
    safe_id = _validate_id_chars(snapshot_id, context="snapshot_id (load_snapshot)", forbidden=_FORBIDDEN_ID_CHARS)
    path = store.root / _SNAP_DIR / f"{safe_id}.json"
    resolve_under_root(store.root, path)   # container assertion over the final path
    if not path.exists():
```

### （可选，建议）插入点 4/5 — 读循环兜底
- `store.py:82-83` `_load_json_dir`：`for file_path in sorted(path.glob("*.json"))` 来自 `glob` 本就在 `path` 下，理论上不需断言；**优先级最低，可不做**（glob 不会产生 `..`）。
- `snapshot.py:92` `list_snapshots`：同理，glob 结果天然在目录内，**可不做**。
> 建议：P0 核心做插入点 1/2/3（写边界 + 外部可控读边界 load_snapshot）即可闭环；4/5 是 glob 来源、无外部注入面，列出供执行 agent 判断，倾向不做以免噪声。

### inspiration/store.py（领地内，附带项，建议本轮一并最小加固）
- `inspiration/store.py:59` `(self.raw_dir / f"{source.id}.txt")`、:68 `(self.sources_dir / f"{source.id}.json")`：`source.id` 由 `_unique_source_id` 内部 slug+hash 合成（:161-169，slug 经 `slugify`），**非外部直控**，逃逸风险极低。
- 方案（可选）：写前各加一行 `resolve_under_root(self.root, target)`（root=`self.root`=`content_root/references`）。
- **判断**：这是「统一防线」的完整性收尾，但因 id 内部合成、风险低，**建议执行 agent 视改动量决定**；若做，保持与 store/snapshot 同款一行断言风格。不做也不影响 P0 主结论闭环。

---

## P0 测试建议

1. **复用现有 fixture 风格**（见 `test_snapshot_diff.py:105-130` 的 `_MALICIOUS_IDS` 参数化）：
   - store 侧：构造 `ContentBundle`，把一个 quest/entity 的 `id` 设为 `../../escape`（或直接构造含恶意 id 的 dict 走 `model_validate`），调 `store.save(bundle)`，断言抛 `ValueError`（第一层 `_validate_id_chars` 先命中；若想专测第二层，可临时构造一个绕过第一层但仍逃逸的路径——实际上绕不过，所以第二层是纯兜底，测试主要验「双层都在、合法 id 不被误拦」）。
2. **不误拦回归（关键）**：
   - `store.save(bundle)` 用正常 slug id（`npc_xxx`、`quest_xxx`、`loc_xxx`）→ 不抛、文件正常落盘。
   - `write_snapshot(store)` → 时间戳 id 正常落盘、`load_snapshot` 能读回。
   - **qer 冒号 id 回归**：一个含 `quest_event_ref`（合成 id `q:e:kind`）的 bundle，`save` + `load` 往返不报错（验证聚合路径未被误拦）。现有 `test_chaos_fixes.py:1010` 附近已有相关注释，可在那里加一条。
3. **load_snapshot 既有恶意 id 测试**（`test_snapshot_diff.py:118-131`）应继续全绿（第二层叠加不改变其拒绝行为，只是多一道兜底）。
4. 可在 `test_trust_security.py` 或 `test_snapshot_diff.py` 内补「store 写边界容器断言」用例，保持与现有命名一致。

---

## P3-b 调研结论（仅表述/文档，逻辑零改）

### 检测逻辑现状（injection.py）
- `content/injection.py`：模块级 `_INJECTION_PATTERNS`（中英 + 全角同义扩展 + `---/===/>>>` 分隔注入），`scan_for_injection(text) -> list[str]` 返回命中的 pattern 源串（空=干净）。
- 现有 docstring（injection.py:1-12）**已经诚实**地写了「regex first layer (defense-in-depth), not a guarantee」「OWASP 明说 pattern filter 会漏 sophisticated indirect injection」「我们 surface matches 供人/风险审查，并把 untrusted content 挡在 instruction position 之外」。
- **本任务只需把「真正主防线 = 权限隔离/人审唯一写 canon」这句话补进去并指向证据**，逻辑一行不改。

### 「LLM 产出永不自动落库、人审唯一写 canon」的代码佐证（精确引用）

**A. 唯一写路径在 `pipeline/review.py`（最强证据）**
- 模块 docstring（`pipeline/review.py:1-6`）原文：
  > "Accepting an item is THE write path for AI-produced content: a quest draft is materialised into the content store with `review_status=approved` while `origin=ai_draft` stays untouched, so the provenance trail survives approval."
- `decide_review_item(project, item_id, *, decision, operator)`（:37）：
  - `operator` 必填（:46-47 `if not operator.strip(): raise`）→ **人必须署名**。
  - 只有 `decision == "accepted"` 才走 `project.content_store.save(bundle)`（:80, 102, 118, 132, 153, 168）。
  - 每条写路径前调 `_assert_no_new_accept_errors`（:210-227）：候选若新增确定性审计错误就**拒绝写正典**（含 QUEST_LOGIC 额外 `audit_quest_logic` 门，:94-99）。
  - 决策不可逆（:53-57 已决定的项再 accept/reject 直接 raise）。

**B. 所有 AI 产出落点都是「进 ReviewQueue，不是 store.save」**
- `app/actions.py`：`add_quest_draft`(:394)、`add_world_seed`(:579/662/2044/2202/2312)、`add_character_profile`(:1352)、`add_quest_logic_draft`(:2627)。
- `assist/dialogue_trees.py:154` `add_dialogue_tree`、`assist/flavor.py:158` `add_flavor_batch`。
- `cli/main.py:1161` / `service/api.py:1709` `add_quest_draft`。
- → 即：worldgen / assist / dialogue / flavor 等**所有 LLM 生成出口都只调 `ReviewQueue.add_*`**，把草稿放进 `pending_review` 队列（`assist/review_queue.py`，可落 SQLite 跨会话），**没有任何一条直接 `ContentStore.save`**。

**C. ReviewQueue 本身**（`assist/review_queue.py`）
- `ReviewItem.status` 默认 `"pending_review"`（:38）；`add` 只写 review 表/内存（:53-69），**不碰 content store**。
- 唯一把 `pending_review` → 正典的转换在 `pipeline/review.py::decide_review_item`，且经过人审 + 审计门。

**D. injection 的角色**（与上面呼应）
- `scan_for_injection` 的两个消费方都是**信号/标记**，不阻断、不写正典：
  - `audit/rules/security_rules.py:19` `PromptInjectionRule.check` → 命中则 `yield Issue`（供人审查的审计项），不阻断。
  - `inspiration/store.py:65-67` → 命中则 `source.metadata["injection_flagged"] = "true"`（打标记），仍照常入库为**参考资料**（references，不是 canon），等人决定。
- 这正好佐证「正则是信号层」：它产出的是给人看的信号，最终写 canon 与否由人审 + 审计门把关。

### P3-b 表述材料（给执行 agent 直接用，docstring 措辞建议）
在 `injection.py` 模块 docstring 末尾（现有 "Honest scope" 段之后）补一段，意思固定为：

> 正则是**尽力而为的信号层**（surface signals for human/risk review），**不是注入的主防线**。真正的防线是**架构上的权限隔离 + human-in-the-loop**：LLM 产出永远不会自动写入正典（canon）——所有 AI 草稿只进 `ReviewQueue`（`assist/review_queue.py`，状态 `pending_review`），唯一把草稿落入 content store 的路径是 `pipeline/review.py::decide_review_item`，它要求具名 operator 人工接受、并通过确定性审计门（`_assert_no_new_accept_errors`）。因此即便某条注入文本漏过本正则，它也只能停在审查队列里、由人决定，而不能擅自改变正典或进入 instruction position。

- 可在 `scan_for_injection` 函数 docstring 加一句指针：「This is the signal layer; the authoritative defense is the human-review write boundary (see `pipeline/review.py`).」
- **红线遵守**：检测逻辑（`_INJECTION_PATTERNS` / `scan_for_injection` 返回值）**一字不改**；现有所有测试（`test_t2_hardening.py:294-383`、`test_audit_security_rules.py`、`test_term_injection.py`、`test_reference_retrieval.py`）不受影响。

---

## 一句话可行性结论
**完全可行且低风险**：P0 只需在 store/snapshot 三个最终 `{id}.json` 路径处各叠加一行 `resolve_under_root(<root>, target)` 容器断言（复用已存在的 canon 实现，保留 `_validate_id_chars` 友好第一层），合法 id（含时间戳 snapshot、走 jsonl 不进文件名的 qer 冒号 id）经确认不会误拦；P3-b 纯 docstring 表述，代码库已用 `pipeline/review.py::decide_review_item`（"THE write path"）+ `ReviewQueue` 充分坐实「LLM 永不自动写 canon、人审唯一写路径」，逻辑零改即可把正则准确定位为信号层。
