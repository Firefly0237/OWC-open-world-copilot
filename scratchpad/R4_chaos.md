# R4 · 畸形输入身份（chaos）· 复测结果

身份：单纯想玩、行为不理智的普通用户——往导入/CLI/API 塞畸形输入，看会不会崩/静默吞/报错难懂。
HEAD = Round 3 fixes。venv 实跑（F:\openworld\.venv），复现脚本见 scratchpad/r4_*.py。

---

## 先确认 R3 统一入口覆盖面（前提核对）

`content/normalize.py` 的 `_resolve_id` 确实被 **8 个 kind** 调用（entity / quest /
quest_event_ref / region / poi / dialogue / term / style_guide，grep 9 处含定义本身）。
第 9 个内容类型 **localized_text 不走 `_resolve_id`**——它只做 `_assert_id_is_str_or_none`
类型预检后用 `slug_id` 包一层（`re.sub(r"[^a-z0-9]+","_")` 会把 `/ \ . :` 全洗成 `_`），
所以 localized_text 的 id 天然 path-safe。**这一点是对的，不是 bug。**

→ 也就是说 R3 的「统一收口」只覆盖**通过 normalize.py 这条 ingest 路径**进来的内容。
   下面 BUG-A 正是踩在「还有别的 ingest 路径根本不经过 normalize.py」上。

---

## 🔴 BUG-A（真 bug，高，path 逃逸）：recognize/review 写入路径绕过全部 id 硬化，traversal id 可逃出内容目录

**根因**：R3 把 id 不变量（traversal / 控制字符 / 冒号 / 长度）**只放在 `normalize._resolve_id`**，
模型层（`Entity`/`Quest`/…/`ContentBundle`）的 `id: str` **没有任何 field_validator**
（models.py:58/90/177/… 全是裸 `id: str`）。而项目里存在**第二条独立 ingest 路径**——
`recognize`（识别外部引擎工程文件 → 可编辑 plan → 人审 → 落库），它**完全不经过 normalize**：

- `recognize/table.py:133-155`：`ent_id = _cell(row.get(mapping.id_column))` 直接取用户单元格，
  只判空 + 判重，**无任何字符校验**，塞进 `ProposedEntity(id=ent_id)`。
- `recognize/pipeline.py:154-171` `_to_entity`：`Entity(id=proposed.id, …)` 原样建模，无校验。
- 人审通过时 `pipeline/review.py:106` `ContentBundle.model_validate(payload["bundle"])`
  —— model_validate **不跑 id 校验**；接着 `review.py:118` `content_store.save(bundle)`。
- `store.py:138`：`self._write_json(path / f"{object_id}.json", …)` —— id 直接拼成文件名。

写前闸门 `_assert_no_new_accept_errors`（review.py:210）只看**确定性审计错误**，
而 `audit/rules/` 里**没有任何 id 格式/traversal 规则**（security_rules.py 只查 free-text 注入，
不查 id），所以闸门拦不住畸形 id。

**E2E 证据**（scratchpad/r4_recognize_traversal.py 实跑）：
```
content_root = …/myworld/content
Entity model accepted traversal id: '../../../escaped'   <<< 模型层无守卫
ContentBundle.model_validate kept id: '../../../escaped'
store.save → 实际写出文件：
   …/myworld/content/world/...   （正常）
   …/myworld/escaped.json        <<< 逃出 content/ 两级目录！ESCAPED
```
即 id=`../../../escaped` 的实体被写到了内容根**之上两级**。更多 `../` 即可写到进程可写的任意位置
（覆盖 `.git`、其它世界、用户文件…）。这正是 R3 在 normalize 里堵的那个洞，但 R3 **只堵了 normalize 这一条路**。

**触达面（非死代码）**：`recognize_content_action` 挂在 REST `POST …/recognize`（api.py:3096），
`recognize_import_action`/CLI 也走同一 `_recognize_finish`→`plan_to_bundle`→ReviewQueue→review apply。
同样绕过 normalize 的 review-apply 分支还有 CHARACTER_PROFILE / FLAVOR_BATCH / DIALOGUE_TREE / WORLD_SEED
（review.py:106/122/136/156，全部 `Model.model_validate(payload)` 后 `store.save`）。

**真 bug vs 已兜底**：**真 bug**。是 R3「统一收口」的覆盖盲区——收口只在 normalize 入口，
没有在**写盘边界**（`store._write_json_dir`）或模型层做最后一道兜底。zip 导入路径
（workspaces.py:132-148）有独立的 raw-name + resolved-path 双重 traversal 检查所以安全，
但 recognize/review 这条**没有**对应防线。

**建议**（不在我职责，仅供参考）：在 `ContentStore._write_json_dir` 拼文件名前对 `object_id`
做一次和 `_validate_id_chars` 同源的兜底校验（或给模型 id 加 field_validator），
让所有写盘路径共享同一不变量，而不是依赖每条 ingest 路径各自记得调 normalize。

---

## 🟡 BUG-B（半 bug / 设计权衡 + 静默丢失，中）：localized_text 的 `id` 列（印尼语 ISO-639-1=`id`）被静默丢弃

**根因**：normalize.py:606 把 `id` 放进 `_LOCALE_RESERVED_KEYS`，使任何名为 `id` 的列**永不**被当作 locale。
但 `id` 恰好是印尼语的 ISO-639-1 码。后果（r4_followup.py 实跑）：

- `{"text_key":"greeting","en":"hello","id":"halo"}` → 只产出 **1 行**（en）；
  印尼语 `halo` 被丢，且更糟：因 `len(locale_values)==1`，该 en 行的 id 反而取了 `raw.data["id"]="halo"`
  → 落成 `loc_halo`（en 的行 id 由印尼语文本派生，交叉污染）。
- `{"text_key":"greeting","id":"halo"}`（印尼语作唯一翻译列）→ **产出 0 行，完全静默丢失**。

`audit/rules/import_rules.py` 只检 id 冲突，**没有「丢列/未识别列」告警**，所以这个丢失**全程无任何提示**。

**真 bug vs 设计**：这是 R3 为修「`id` 列伪造假 locale 行」而引入的**有意权衡**（代码注释 602-605 已承认
`id`/印尼语撞码）。「列名叫 id 的到底是行标识还是印尼语翻译」本就二义，选行标识是站得住的。
**但**——静默丢失这一面踩了红线「不静默降级」。归类为**已知设计权衡 + 文档/告警缺口**，不是硬 bug；
若要修，最小做法是丢弃 `id` 列翻译时附一条 warning（而非沉默）。**不夸大严重度。**

---

## 🟡 BUG-C（不一致，低）：locale 白名单只管「列检测」，显式 `locale` 字段完全不校验

**根因**：ISO-639-1 白名单只在 `_looks_like_locale`（列名探测）里用；而**显式 `locale` 字段值**
（dialogue 的 `locale`、localized_text 的 `locale` 字段）**从不过白名单**。证据（r4_locale_asym.py）：

```
显式 locale='zz'        -> 存 'zz'
显式 locale='../slip'   -> 存 '../slip'      （localized_text 走 slug，path 安全）
显式 locale='NOTALOCALE'-> 存 'NOTALOCALE'
但同名“列” zz/qq/xx     -> 被白名单丢弃（存 []）
dialogue locale='zz'    -> 存 'zz'           （DialogueRef.locale 也无白名单）
```

**真 bug vs 设计**：**不一致 / 兜底不全**，但**不是安全漏洞**——这些 locale 值不进文件名
（localized_text id 走 slug；dialogue id 也走 `_resolve_id`，locale 只是字段）。
影响是「简报里宣称『locale 列必须是已知语言码』的保证只兑现了一半」，用户可能误以为 locale 被校验了。
低优先；要修就把白名单也用到显式 locale 字段（注意别误拒 `zh-CN` 大小写/region 形式）。

---

## ✅ 验证 OK 的点（攻击未果，确认 R3 兜底有效）

按简报要求双向攻击，下列均**正确**：

**绕过攻击（被正确拒绝/兜底）**
- quest_event_ref 显式 id 含冒号 `evil:colon` → **正确强拒**（合成 separator 放行只对 fallback 生效，
  显式 id 仍走严格集）。且 qer 存进 `event_refs.jsonl` 而非 `{id}.json`，合成冒号 id 对 path **无害**（r4_followup.py 确认）。
- entity/quest/… 8 kind 经 normalize：NUL/控制字符(\n,\x00-\x1f)、`..`、`/ \ . :`、`...`、>256 长度 → 全部正确拒绝。
- markdown 显式 id（`Name (some-id)`）→ 经 entity 走 `_resolve_id`，含 `/`/`:` 会被拒（覆盖到了）。
- 深嵌套 JSON（depth 5000）→ `RecursionError` 被转成友好中文 ValueError（json.py:49-56，实跑确认，不再裸抛）。
- JSON 顶层标量、整文件非法 JSON、jsonl 单行非法 → 均友好域错误。
- 非 ASCII / emoji / DEL(0x7f) / 全角斜杠(U+FF0F) / 全角句点(U+FF0E) 作 id → **被接受但 path-safe**：
  全角字符不是任何 OS 的路径分隔符，save→load roundtrip 正常落在 entities/ 下（r4_save_path.py 实跑），
  **非 bug**（仅观感怪）。注：DEL 0x7f 未被控制字符检查拦（只拦 <32），但无 path 危害。

**误拒攻击（合法输入未被误拒）**
- 全部测试的合法 ISO-639-1 码（zh/en/ja/ko/vi/th/cy/ga/eu/fo/kl/ie/ia/io/vo + 区域形式 zh-cn/en-us/pt-br
  + 大小写 zh-CN/EN）→ `_looks_like_locale` **全部正确放行**，无误拒。
- id 恰好 256 长 → 放行；257 → 拒绝（边界正确）。
- 全角内容（name=`ＡＢＣ`）作**内容**（非 id）→ 正确接受、不误拒。
- 显式 CJK id（`赵云`）→ 接受、roundtrip 正常。
- locale='id'（印尼语，作显式 locale 字段）→ 正确接受（与 BUG-B 的「列」路径区分开）。
- text_match NFKC 折叠：只用于**离线降级 lexical 检索打分**（text_match.py:53/73/85），
  **不触及 id / canonical 落库**——确认 NFKC 没污染存储层，故「误合并语义不同字符」对 canon 无影响，
  属已诚实标注的 degraded 路径，**非 bug**。

---

## 一句话总结
R3 把 id 硬化「统一收口」到 normalize 入口是对的，但收口只覆盖 normalize 这一条 ingest 路；
**recognize/人审落库这条独立路径完全绕过它，traversal id（`../../../escaped`）能真实写出内容目录之外
（已 E2E 复现）——这是本轮唯一的真 bug（高），根因是兜底放在了入口而非写盘边界/模型层**；
另有印尼语 `id` 列静默丢失（设计权衡+告警缺口，中）和显式 locale 字段不过白名单（不一致，低）两个次要项。
