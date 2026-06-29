# R4 · 剧情策划（文笔/可用性）视角复测结果

身份：只懂文笔、不懂技术的资深游戏剧情策划。
关注：非技术策划顺不顺手、写作辅助靠不靠谱、报错看不看得懂、术语吓不吓人。
被测：HEAD=73d4d04（R3 fixes）。

---

## 一、R3 我领域两处修复——逐一验证 OK

### ✅ R3-E1 ReviewPage 全角引号坏页 bug —— 真修好了
- 证据：`frontend/src/pages/ReviewPage.vue:402` 现为半角 `class="muted ct-desc"`（R3 前是全角 `class=”muted ct-desc”`）。
- 影响面确认：该处是「AI 评审准确度参考 → 漏检解释」段落（L401-404）。全角引号会让浏览器把整个 `class` 当成畸形属性、样式丢失，进而那段「漏检」说明渲染坏掉。现已生效。
- 周边文案以策划视角通读（L391-431）：「漏检/一致率/漏检率/需复核（评审通过却被退回）」措辞通顺、无黑话，置信区间 `[x–y]` 也读得懂。良好。

### ✅ R3-E2 创作工坊「lint」黑话改写 —— 真改了
- 证据：`frontend/src/pages/CreationPage.vue:282` `{{ dlgResult.lint }} lint` → `{{ dlgResult.lint }} 条待复核`；`:366` `X 条被 lint 拦下` → `X 条因引用越界被拦下`。
- 评估：比原「lint」好懂得多。`待复核` 与旁边「节点 / 结构问题」并列、用 amber 警告色，语气从「问题」软化为「待复核」属合理的非技术化。
- 软建议（非 bug）：「引用越界」对纯文笔策划仍略抽象，可考虑「X 条因提到了世界里不存在的设定，已拦下」之类。不影响功能，留作打磨。

---

## 二、R3 回归压测——全角标点写进属性：已扫干净（验证 OK）

- 方法：用脚本对 `frontend/src/**`（.vue/.ts/.css/.html）精扫会破坏「标签/属性」解析的全角标点（“ ” ‘ ’ ＝ ＜ ＞），逐行定位。脚本与结果见 `scratchpad/fwscan.txt`。
- 结果：除 R3 已修的 ReviewPage 那处外，**全仓再无第二处全角引号/尖括号写进 HTML 属性**。
- 唯一命中 `GenesisPage.vue:384` 的全角等号 `＝`，是 placeholder **文本值内部**的中文标点（"留空＝按核心想法自动检索"），不在属性定界处、不破坏渲染——**误报，无需动**。
- 结论：E 队声称的 "full-repo sweep for the same" 确实做到了，全角标点 bug 已根除。

---

## 三、找到的真问题（黑话泄漏，R3 sweep 漏网，与已修 lint 同类）

### 【轻微 · 真 bug（文案/可用性）】后端字段名 `timeline_order` 泄漏进多处面向用户的中文正文
- 根因：R3-E 把「lint」黑话只清了 CreationPage，**同性质的内部 snake_case 字段名 `timeline_order` 散落在时间线页与新手引导多处未清**。这些不是脚本/数据键，而是直接渲染给用户读的中文句子里夹着英文字段名。
- 证据（均为用户可见正文，非 mono/技术区，已逐处坐实）：
  - `frontend/src/pageHelp.ts:84`（新手引导 ? 提示）：「任务与事件按 **timeline_order** 排到一条轴上，编年违例标红。」
  - `frontend/src/pages/TimelinePage.vue:534`（空状态首屏引导，普通 `<p class="muted">`）：「还没有带时序的任务或事件。给任务填上 **timeline_order**，这条轴就会长出来。」
  - `frontend/src/pages/TimelinePage.vue:743`（编辑面板字段标签 `<span>`）：「顺序（**timeline_order**）」
  - `frontend/src/pages/TimelinePage.vue:839`（分组标题 `<span class="t">`）：「未定序（缺 **timeline_order**）」
- 策划视角影响：非技术策划读到中文句子里突然冒出 `timeline_order` 会懵（"这是哪个英文要我去填？"），尤其 L534 是进入空时间线第一眼的引导，最该说人话。
- 真 bug vs 已兜底：**真问题**。与 R3 已认定要修的「lint」黑话完全同类、同性质，只是 sweep 没覆盖到 timeline 页/pageHelp。不是误报、不是已知限制。
- 修法建议：把可见正文里的 `timeline_order` 替换为「时序号 / 时间顺序」等中文（注释 TimelinePage.vue:8 与 API payload 键 L390/397/412/420 等保持英文不动——那些用户看不到）。

---

## 四、查过但判定「非 bug / 不属我领域回归」的点（诚实标注）

- **lint / payload / schema / normalize 其它命中**：全部在 `<script>` 区（TS 接口名、变量名、eslint 注释），用户看不到——不算黑话泄漏。仅 CreationPage:282 那处面向用户的已被 R3 改对。
- **normalize.py 的 R3 新错误消息**（"id must not be blank…"、"duplicate id… resolve the conflict"、locale/deep-nested guided error 等）：**全部英文、不含中文**。前端 `api.ts:62` 的转译只放行「含中文且 ≤160 字、无 stack 标记」的后端消息，这些英文消息命中 L65 兜底「这一步没有成功，请重试…」——**不会把英文术语原样吓到前端用户**。验证 OK（这是好的设计）。
- **CLI 全局 error boundary**（`cli/main.py:58-67`）把异常以 `{"error":…,"type":…}` JSON 原文打到 stderr：对非技术策划确实不友好，但 CLI 是技术/运维入口、简报已把「CLI 安装分发」列为产品战略边界，且非 R3 引入——**不报为新 bug**。
- **`target_ref`/`object_ref` 等合成引用值**（如 ImpactPage:105 placeholder「目标引用，如 entity:npc_x」、AuditPage 引用对象、CompliancePage 等）：显示的是带 `mono` 样式的对象标识、且多有中文标签包裹，属面向技术倾向用户的既有设计，非 R3 回归——不报。
- **embedding `degraded` 标记 / QA gate 改名 citation-existence**：停留在后端机读字段，前端未把这些标记当文案直接透出；离线/未接入用的是「未接入模型」「离线」等 R2 已有友好文案——**无新的降级文案泄漏**。AskPage 引用展示符合 pageHelp「答案附依据」承诺。
- **pageHelp.ts / GuidedTour.vue 整体**：文笔克制、术语友好（API Key 有「账号凭证」包裹等），除上面 timeline_order 一处外无黑话泄漏。
- **已修勿重复**（导出下载、新手引导跳转、参考模式 hint、API 断线提示）：未触碰。

---

## 总结一句
R3 我领域两处修复（ReviewPage 全角引号坏页、CreationPage「lint」黑话）都真修好了、全角标点全仓已扫干净、无新渲染/文案回归；唯一残留真问题是同性质的后端字段名 `timeline_order` 仍泄漏进时间线页+新手引导 4 处用户可见中文正文（轻微，sweep 漏网），建议一并换成中文说法。
