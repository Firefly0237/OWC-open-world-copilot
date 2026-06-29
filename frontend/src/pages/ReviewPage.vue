<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, apiPost, currentOperator, currentProject, llmParams, setCurrentOperator } from "../api";
import { notifyError, notifyOk } from "../toast";
import PageHead from "../components/PageHead.vue";
import Modal from "../components/Modal.vue";

// Drafts that can be regenerated from reviewer feedback rather than only accepted/rejected.
const REVISABLE = new Set(["quest_draft", "character_profile", "dialogue_tree", "world_seed"]);

// ---- C-4 契约: GET /projects/{p}/review_items/{id}:context ----
interface PayloadStageSummary {
  id: string;
  description: string; // truncated to 100 chars
}

interface PayloadSummary {
  title: string | null;
  objective: string | null;
  stages: PayloadStageSummary[];
  summary: string | null;
}

interface CalibrationContext {
  item_type: string;
  false_pass_rate: number | null; // null when sufficient_sample=false
  sample_size: number;
  sufficient_sample: boolean;
}

interface ReviewItemContext {
  item_id: string;
  item_type: string;
  status: string;
  payload_summary: PayloadSummary;
  issue_refs: string[];
  critic_verdict: "pass" | "revise" | null;
  critic_score: number | null;
  refine_trail_last_reflection: string | null;
  calibration_context: CalibrationContext;
}

// per-item context state: keyed by item.id
const contextCache = ref<Record<string, ReviewItemContext | "loading" | "error">>({});
const contextOpen = ref<Record<string, boolean>>({});

async function toggleContext(itemId: string): Promise<void> {
  // toggle open/close
  contextOpen.value[itemId] = !contextOpen.value[itemId];
  // only fetch if not already fetched
  if (contextOpen.value[itemId] && contextCache.value[itemId] == null) {
    contextCache.value[itemId] = "loading";
    try {
      const data = await apiGet<ReviewItemContext>(
        `/projects/${currentProject()}/review_items/${itemId}:context`,
      );
      contextCache.value[itemId] = data;
    } catch (e) {
      contextCache.value[itemId] = "error";
      notifyError(e);
    }
  }
}

function contextFor(itemId: string): ReviewItemContext | null {
  const v = contextCache.value[itemId];
  if (v && v !== "loading" && v !== "error") return v as ReviewItemContext;
  return null;
}

function isContextLoading(itemId: string): boolean {
  return contextCache.value[itemId] === "loading";
}

function isContextError(itemId: string): boolean {
  return contextCache.value[itemId] === "error";
}

// UI helpers
function verdictLabel(v: "pass" | "revise" | null): string {
  if (v === "pass") return "通过";
  if (v === "revise") return "建议修订";
  return "（未经 AI 评审）";
}

function verdictClass(v: "pass" | "revise" | null): string {
  if (v === "pass") return "verdict-pass";
  if (v === "revise") return "verdict-revise";
  return "verdict-null";
}

function pctScore(s: number | null): string {
  if (s == null) return "—";
  return `${Math.round(s * 100)}`;
}

function falsePassDisplay(ctx: CalibrationContext): string {
  if (!ctx.sufficient_sample) return "样本不足（< 20 条），暂无可靠统计";
  if (ctx.false_pass_rate == null) return "—";
  return `${Math.round(ctx.false_pass_rate * 100)}%`;
}

interface ReviewItem {
  id: string;
  item_type: string;
  object_ref: string;
  payload: Record<string, unknown>;
}

const items = ref<ReviewItem[]>([]);
const operator = ref(currentOperator());

const TYPE_LABELS: Record<string, string> = {
  quest_draft: "任务草稿",
  bark_variant: "台词变体",
  patch_candidate: "修复补丁",
  world_seed: "世界草案",
  import_draft: "提炼草案",
  dialogue_tree: "对话树",
  flavor_batch: "物案批次",
  character_profile: "角色卡",
};

const FIELD_LABELS: Record<string, string> = {
  description: "简介",
  summary: "梗概",
  objective: "目标",
  profile: "角色卡",
  relations: "关系",
  stages: "阶段",
  rewards: "奖励",
  plot_beats: "剧情节拍",
  beats: "节拍",
  entities: "实体",
  factions: "阵营",
  regions: "区域",
  characters: "人物",
  premise: "主轴",
  tone: "基调",
  counts: "统计",
  gaps: "待补缺口",
  items: "条目",
  nodes: "节点",
  lines: "台词",
  suggested_relations: "建议关联",
  text: "内容",
  body: "正文",
  giver_npc: "发布者",
  location: "地点",
  unsupported: "原文未见",
};
const PROFILE_LABELS: Record<string, string> = {
  appearance: "外貌",
  personality: "性格",
  backstory: "背景故事",
  motivation: "动机与目标",
  abilities: "能力与专长",
  weakness: "弱点与恐惧",
  voice: "说话方式",
};

// Aspects the reviewer can tick to steer a revision — the "where to revise" the creation form had.
const REVISE_ASPECTS: Record<string, string[]> = {
  quest_draft: ["目标更清晰", "阶段更合理", "奖励调整", "对白润色", "难度调整", "更贴合设定"],
  character_profile: ["口吻语气", "动机目标", "背景故事", "性格", "关系", "能力与弱点"],
  world_seed: ["主轴张力", "阵营设定", "区域设计", "角色", "任务线", "整体基调"],
  dialogue_tree: ["分支结构", "台词口吻", "选择项设计"],
};
function aspectsFor(type: string): string[] {
  return REVISE_ASPECTS[type] ?? ["更具体", "更专业", "更贴合设定"];
}

function asObj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function itemTitle(item: ReviewItem): string {
  const p = item.payload;
  const entity = asObj(p.entity);
  const cand =
    (entity.name as string) ||
    (p.title as string) ||
    (p.name as string) ||
    (p.source_title as string) ||
    (typeof p.summary === "string" ? p.summary.slice(0, 28) : "") ||
    item.object_ref;
  return String(cand);
}

interface Section {
  label: string;
  text?: string;
  rows?: { k: string; v: string }[];
  chips?: string[];
  list?: string[];
}

const SKIP_KEYS = new Set([
  "id",
  "ref",
  "object_ref",
  "kind",
  "item_type",
  "refs",
  "dialogue_refs",
  "localization_keys",
  "name",
  "title",
  "source_title",
  "metadata",
  "graph_pos",
  "auto_review_incomplete",
  // internal / echo-of-input fields that aren't worth a reviewer's eyes
  "brief",
  "bundle",
  "refine_trail",
  "project_context_refs",
  "origin",
  "review_status",
  "seed_id",
  "style_guide_id",
  "timeline_order",
  "stats",
]);

function describeRow(o: Record<string, unknown>): string {
  if (o.kind && o.target) return `${o.kind} → ${o.target}`;
  if (o.source && o.target) return `${o.source} ${o.kind ?? "→"} ${o.target}`;
  return String(
    o.name ?? o.title ?? o.summary ?? o.text ?? o.description ?? o.canonical ?? JSON.stringify(o),
  );
}

function sectionsOf(item: ReviewItem): Section[] {
  const p = item.payload;
  const out: Section[] = [];
  for (const [key, val] of Object.entries(p)) {
    if (SKIP_KEYS.has(key) || val == null || val === "") continue;
    const label = FIELD_LABELS[key] ?? key;
    if (typeof val === "string" || typeof val === "number") {
      out.push({ label, text: String(val) });
    } else if (Array.isArray(val)) {
      if (!val.length) continue;
      if (typeof val[0] === "string") out.push({ label, chips: val.map(String) });
      else out.push({ label, list: val.map((o) => describeRow(asObj(o))) });
    } else if (typeof val === "object") {
      if (key === "entity") {
        const e = asObj(val);
        if (e.description) out.push({ label: "简介", text: String(e.description) });
        continue;
      }
      const rows = Object.entries(asObj(val))
        .filter(([, v]) => v != null && v !== "")
        .map(([k, v]) => ({
          k: PROFILE_LABELS[k] ?? FIELD_LABELS[k] ?? k,
          v: typeof v === "string" ? v : JSON.stringify(v),
        }));
      if (rows.length) out.push({ label, rows });
    }
  }
  return out;
}

async function refresh(): Promise<void> {
  try {
    const body = await apiGet<{ items: ReviewItem[] }>(`/projects/${currentProject()}/review_items`);
    items.value = body.items;
  } catch (e) {
    notifyError(e);
  }
  await loadCalibration();
}

function requireOperator(): boolean {
  if (!operator.value.trim()) {
    notifyError("先在上方填写你的名字（用于记录谁做了审阅）。");
    return false;
  }
  setCurrentOperator(operator.value.trim());
  return true;
}

async function decide(item: ReviewItem, decision: "accepted" | "rejected"): Promise<void> {
  if (!requireOperator()) return;
  try {
    const body = await apiPost<{ written_ref: string | null }>(
      `/projects/${currentProject()}/review_items/${item.id}:decide`,
      { decision, operator: operator.value.trim() },
    );
    notifyOk(
      decision === "accepted"
        ? `已钤印入档${body.written_ref ? `：${body.written_ref}` : ""}。`
        : "已驳回，草稿就地焚毁。",
    );
  } catch (e) {
    notifyError(e);
  }
  await refresh();
}

// ---- revise modal ----
const reviseItem = ref<ReviewItem | null>(null);
const reviseAspects = ref<string[]>([]);
const reviseNote = ref("");
const revising = ref(false);

function openRevise(item: ReviewItem): void {
  if (!requireOperator()) return;
  reviseItem.value = item;
  reviseAspects.value = [];
  reviseNote.value = "";
}
function toggleAspect(a: string): void {
  const i = reviseAspects.value.indexOf(a);
  if (i >= 0) reviseAspects.value.splice(i, 1);
  else reviseAspects.value.push(a);
}
const canRevise = computed(() => reviseAspects.value.length > 0 || reviseNote.value.trim().length > 0);

async function submitRevise(): Promise<void> {
  const item = reviseItem.value;
  if (!item || !canRevise.value || revising.value) return;
  const parts: string[] = [];
  if (reviseAspects.value.length) parts.push(`重点修订：${reviseAspects.value.join("、")}。`);
  if (reviseNote.value.trim()) parts.push(reviseNote.value.trim());
  revising.value = true;
  try {
    await apiPost(`/projects/${currentProject()}/review_items/${item.id}:revise`, {
      feedback: parts.join(" "),
      operator: operator.value.trim(),
      ...llmParams(),
    });
    reviseItem.value = null;
    notifyOk("已按你的意见重写，仍在审阅台等你定夺。");
  } catch (e) {
    notifyError(e);
  } finally {
    revising.value = false;
  }
  await refresh();
}

// --- reviewer calibration: how well did the critic's verdict predict the human's decision? ---
interface Calibration {
  sample_size: number;
  matrix: Record<string, number>;
  false_pass_rate: number | null;
  false_pass_rate_ci: number[] | null;
  agreement_rate: number | null;
  mean_score_accepted: number | null;
  mean_score_rejected: number | null;
  sufficient_sample: boolean;
  min_sufficient_sample: number;
  false_pass_items: { item_id: string; object_ref: string; critic_score: number | null }[];
}
const calib = ref<Calibration | null>(null);
const showCalib = ref(false);

async function loadCalibration(): Promise<void> {
  try {
    calib.value = await apiGet<Calibration>(`/projects/${currentProject()}/review/calibration`);
  } catch {
    calib.value = null; // calibration is diagnostic; never block the queue on it
  }
}

const pct = (x: number | null | undefined): string =>
  x == null ? "—" : `${Math.round(x * 100)}%`;
const scoreText = computed(() => {
  const c = calib.value;
  if (!c) return "—";
  const a = c.mean_score_accepted == null ? "—" : c.mean_score_accepted.toFixed(2);
  const r = c.mean_score_rejected == null ? "—" : c.mean_score_rejected.toFixed(2);
  return `${a} / ${r}`;
});

onMounted(refresh);
</script>

<template>
  <section>
    <PageHead overline="REVIEW" title="审阅台" purpose="AI 产物逐条采纳或退回，决定后不可更改。" />
    <div class="operator">
      <input v-model="operator" placeholder="你的名字（必填，用于记录审阅操作）" />
      <button class="ghost" @click="refresh">刷新队列</button>
      <span class="muted count">{{ items.length }} 条待审</span>
    </div>

    <div v-if="calib && calib.sample_size > 0" class="pane calib">
      <button class="calib-toggle" @click="showCalib = !showCalib">
        <span class="ct-label">AI 评审准确度参考 · 已有 {{ calib.sample_size }} 条数据</span>
        <span
          v-if="calib.false_pass_rate != null"
          class="ct-fp"
          :class="{ warn: calib.false_pass_rate > 0 }"
        >
          漏检 {{ pct(calib.false_pass_rate) }}
        </span>
        <span class="ct-caret" :class="{ open: showCalib }">▾</span>
      </button>
      <div v-if="showCalib" class="calib-body">
        <p class="muted ct-desc">
          这里显示 AI 自动评审建议（通过/建议修订）和你最终决定的吻合程度。重点看「漏检」：AI 说通过、但你最终退回的草稿比例越低说明 AI 越可靠。数字仅供参考，不影响你的审阅决定。
        </p>
        <p v-if="!calib.sufficient_sample" class="muted ct-thin">
          样本偏少（少于 {{ calib.min_sufficient_sample }} 条），下列比率仅供参考。
        </p>
        <div class="calib-grid">
          <div class="cg">
            <span class="k">一致率</span><span class="v">{{ pct(calib.agreement_rate) }}</span>
          </div>
          <div class="cg">
            <span class="k">漏检率</span>
            <span class="v">
              {{ pct(calib.false_pass_rate) }}
              <small v-if="calib.false_pass_rate_ci" class="ci">
                [{{ pct(calib.false_pass_rate_ci[0]) }}–{{ pct(calib.false_pass_rate_ci[1]) }}]
              </small>
            </span>
          </div>
          <div class="cg">
            <span class="k" title="AI 对你采纳和退回的草稿打的平均分，差距越大说明 AI 分辨能力越强">AI 评分均值（已采纳/已退回）</span><span class="v">{{ scoreText }}</span>
          </div>
        </div>
        <div v-if="calib.false_pass_items.length" class="fp-list">
          <span class="k">需复核（评审通过却被退回）</span>
          <span v-for="f in calib.false_pass_items" :key="f.item_id" class="chip mono">
            {{ f.object_ref }}<small v-if="f.critic_score != null"> · {{ f.critic_score.toFixed(2) }}</small>
          </span>
        </div>
      </div>
    </div>

    <p v-if="!items.length" class="muted empty">暂无待审阅的草稿。</p>

    <TransitionGroup name="card" tag="div" class="queue">
      <article v-for="item in items" :key="item.id" class="pane card reveal">
        <header class="r-head">
          <div class="r-title">
            <span class="r-type">{{ TYPE_LABELS[item.item_type] ?? item.item_type }}</span>
            <h2>{{ itemTitle(item) }}</h2>
          </div>
          <span class="r-ref mono" :title="item.object_ref" style="cursor:help">ID</span>
        </header>

        <div class="r-body">
          <div v-for="(sec, i) in sectionsOf(item)" :key="i" class="sec">
            <span class="sec-label">{{ sec.label }}</span>
            <p v-if="sec.text" class="sec-text">{{ sec.text }}</p>
            <div v-if="sec.rows" class="sec-rows">
              <div v-for="(row, j) in sec.rows" :key="j" class="sec-row">
                <span class="rk">{{ row.k }}</span><span class="rv">{{ row.v }}</span>
              </div>
            </div>
            <ul v-if="sec.list" class="sec-list">
              <li v-for="(li, j) in sec.list" :key="j">{{ li }}</li>
            </ul>
            <div v-if="sec.chips" class="sec-chips">
              <span v-for="(c, j) in sec.chips" :key="j" class="sec-chip">{{ c }}</span>
            </div>
          </div>
        </div>

        <!-- 上下文面板：展示聚合的"为什么"信息供审阅者判断 -->
        <div class="ctx-panel">
          <button class="ctx-toggle" @click="toggleContext(item.id)">
            <span class="ctx-toggle-label">审阅上下文</span>
            <span v-if="isContextLoading(item.id)" class="ow-spinner ctx-spinner"></span>
            <span class="ctx-caret" :class="{ open: contextOpen[item.id] }">▾</span>
          </button>

          <div v-if="contextOpen[item.id]" class="ctx-body">
            <!-- 加载中 -->
            <div v-if="isContextLoading(item.id)" class="ctx-loading">
              <span class="ow-spinner"></span>
              <span class="muted">加载上下文中…</span>
            </div>

            <!-- 加载失败 -->
            <p v-else-if="isContextError(item.id)" class="muted ctx-err">上下文加载失败，请稍后重试。</p>

            <!-- 加载成功 -->
            <template v-else-if="contextFor(item.id)">
              <!-- 1. 内容摘要 (payload_summary) -->
              <div class="ctx-section">
                <span class="ctx-section-label">内容摘要</span>
                <template v-if="contextFor(item.id)!.payload_summary.title">
                  <p class="ctx-field-row">
                    <span class="ctx-k">标题</span>
                    <span class="ctx-v">{{ contextFor(item.id)!.payload_summary.title }}</span>
                  </p>
                </template>
                <template v-if="contextFor(item.id)!.payload_summary.objective">
                  <p class="ctx-field-row">
                    <span class="ctx-k">目标</span>
                    <span class="ctx-v">{{ contextFor(item.id)!.payload_summary.objective }}</span>
                  </p>
                </template>
                <template v-if="contextFor(item.id)!.payload_summary.stages.length">
                  <div class="ctx-field-row ctx-stages">
                    <span class="ctx-k">前 2 阶段</span>
                    <div class="ctx-stages-list">
                      <div
                        v-for="stage in contextFor(item.id)!.payload_summary.stages"
                        :key="stage.id"
                        class="ctx-stage"
                      >
                        <span class="ctx-stage-id mono">{{ stage.id }}</span>
                        <span class="ctx-stage-desc">{{ stage.description }}</span>
                      </div>
                    </div>
                  </div>
                </template>
                <template v-if="contextFor(item.id)!.payload_summary.summary">
                  <p class="ctx-field-row">
                    <span class="ctx-k">摘要</span>
                    <span class="ctx-v">{{ contextFor(item.id)!.payload_summary.summary }}</span>
                  </p>
                </template>
              </div>

              <!-- 2. 关联议题 (issue_refs) — 显示可读短标签，完整 ID 作 title tooltip -->
              <div v-if="contextFor(item.id)!.issue_refs.length" class="ctx-section">
                <span class="ctx-section-label">关联议题</span>
                <div class="ctx-chips">
                  <span
                    v-for="ref in contextFor(item.id)!.issue_refs"
                    :key="ref"
                    class="chip mono ctx-issue-chip"
                    :title="ref"
                  >
                    {{
                      (() => {
                        const bare = ref.replace(/^issue:/, '');
                        // 取最后一段下划线分隔部分作为人类可读标签（保留前 20 字符）
                        const parts = bare.split('_');
                        const label = parts.length > 2
                          ? parts.slice(-2).join('_')
                          : bare;
                        return label.length > 20 ? label.slice(0, 18) + '…' : label;
                      })()
                    }}
                  </span>
                </div>
              </div>

              <!-- 3. AI 评审结果 (critic_verdict / critic_score) -->
              <div class="ctx-section">
                <span class="ctx-section-label">AI 评审</span>
                <div class="ctx-verdict-row">
                  <span
                    class="ctx-verdict"
                    :class="verdictClass(contextFor(item.id)!.critic_verdict)"
                  >
                    {{ verdictLabel(contextFor(item.id)!.critic_verdict) }}
                  </span>
                  <span v-if="contextFor(item.id)!.critic_score != null" class="ctx-score num">
                    评分 {{ pctScore(contextFor(item.id)!.critic_score) }}<small>/100</small>
                  </span>
                </div>
              </div>

              <!-- 4. AI 精修最后一轮反思 (refine_trail_last_reflection) -->
              <div class="ctx-section">
                <span class="ctx-section-label">精修反思</span>
                <p v-if="contextFor(item.id)!.refine_trail_last_reflection" class="ctx-reflection">
                  {{ contextFor(item.id)!.refine_trail_last_reflection }}
                </p>
                <p v-else class="muted ctx-null-hint">（此草稿未经 AI 精修）</p>
              </div>

              <!-- 5. 校准上下文 (calibration_context) -->
              <div class="ctx-section ctx-calib">
                <span class="ctx-section-label">历史漏检参考</span>
                <div class="ctx-calib-grid">
                  <div class="ctx-calib-cell">
                    <span class="ctx-k">类型</span>
                    <span class="ctx-v mono">{{ TYPE_LABELS[contextFor(item.id)!.calibration_context.item_type] ?? contextFor(item.id)!.calibration_context.item_type }}</span>
                  </div>
                  <div class="ctx-calib-cell">
                    <span class="ctx-k">样本数</span>
                    <span class="ctx-v num">{{ contextFor(item.id)!.calibration_context.sample_size }}</span>
                  </div>
                  <div class="ctx-calib-cell">
                    <span class="ctx-k">漏检率</span>
                    <span
                      class="ctx-v"
                      :class="{ 'ctx-fp-warn': contextFor(item.id)!.calibration_context.sufficient_sample && (contextFor(item.id)!.calibration_context.false_pass_rate ?? 0) > 0 }"
                    >
                      {{ falsePassDisplay(contextFor(item.id)!.calibration_context) }}
                    </span>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>

        <footer class="r-actions">
          <button class="primary" @click="decide(item, 'accepted')">采纳入档</button>
          <button class="ghost" @click="decide(item, 'rejected')">驳回</button>
          <button v-if="REVISABLE.has(item.item_type)" class="ghost revise-btn" @click="openRevise(item)">
            请修订…
          </button>
        </footer>
      </article>
    </TransitionGroup>

    <Modal
      :open="reviseItem !== null"
      overline="REVISE"
      :title="reviseItem ? `请修订 · ${itemTitle(reviseItem)}` : ''"
      @close="reviseItem = null"
    >
      <p class="rv-hint muted">勾选要修订的方面，或直接写意见——产品会据此重写，结果仍回审阅台等你定夺。</p>
      <div v-if="reviseItem" class="rv-aspects">
        <button
          v-for="a in aspectsFor(reviseItem.item_type)"
          :key="a"
          type="button"
          class="rv-aspect"
          :class="{ on: reviseAspects.includes(a) }"
          @click="toggleAspect(a)"
        >
          {{ a }}
        </button>
      </div>
      <textarea
        v-model="reviseNote"
        class="rv-note"
        rows="3"
        placeholder="具体意见（可选），例如：让两个阵营更对立、加入失败后果、口吻更冷峻"
      ></textarea>
      <template #footer>
        <button class="ghost" @click="reviseItem = null">取消</button>
        <button class="primary" :disabled="!canRevise || revising" @click="submitRevise">
          {{ revising ? "重写中…" : "提交修订" }}
        </button>
      </template>
    </Modal>
  </section>
</template>

<style scoped>
.calib {
  margin-bottom: 1rem;
  padding: 0;
  overflow: hidden;
}
.calib-toggle {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  width: 100%;
  background: transparent;
  border: none;
  color: var(--ow-ink);
  padding: 0.7rem 1rem;
  cursor: pointer;
  font-size: 0.9rem;
}
.ct-label {
  font-weight: 600;
}
.ct-fp {
  font-size: 0.78rem;
  color: var(--ow-ink-dim);
}
.ct-fp.warn {
  color: var(--ow-flag, #e0653a);
}
.ct-caret {
  margin-left: auto;
  transition: transform 0.2s ease;
  color: var(--ow-ink-dim);
}
.ct-caret.open {
  transform: rotate(180deg);
}
.calib-body {
  padding: 0 1rem 1rem;
  border-top: 1px solid var(--ow-line);
}
.ct-desc,
.ct-thin {
  font-size: 0.8rem;
  margin: 0.7rem 0 0;
}
.calib-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 1.4rem;
  margin: 0.8rem 0;
}
.cg {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}
.cg .k {
  font-size: 0.72rem;
  color: var(--ow-ink-dim);
}
.cg .v {
  font-size: 1.1rem;
  color: var(--ow-gold, var(--ow-ink));
}
.cg .ci {
  font-size: 0.72rem;
  color: var(--ow-ink-dim);
}
.fp-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
}
.fp-list .k {
  font-size: 0.74rem;
  color: var(--ow-ink-dim);
  margin-right: 0.3rem;
}
.fp-list .chip {
  font-size: 0.74rem;
  padding: 0.2rem 0.5rem;
}
.operator {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 1rem;
}
.operator input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  /* FE-2: geometric border-radius to match panel language */
  border-radius: 2px;
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
  width: 14rem;
}
.count {
  font-size: 0.8rem;
  margin-left: auto;
}
.empty {
  padding: 2rem 0;
}

button {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  /* FE-2: was 0.5rem (Notion/Linear roundness). Small clip-path cut gives HSR geometric feel.
     border-radius: 2px as fallback for clip-path-unsupported contexts. */
  border-radius: 2px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
  color: var(--ow-ink);
  padding: 0.5rem 1rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.86rem;
}
button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}
button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
button.ghost:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

.card {
  padding: 1rem 1.15rem;
  margin-bottom: 0.85rem;
}

/* the content being reviewed leads — a real masthead, not a faint subtitle */
.r-head {
  display: flex;
  align-items: flex-start;
  gap: 0.8rem;
  padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--ow-gold-faint);
}
.r-type {
  display: inline-block;
  font-family: var(--ow-overline);
  font-size: 0.66rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ow-violet);
}
.r-title h2 {
  margin: 0.1rem 0 0;
  font-size: 1.22rem;
  color: var(--ow-ink);
}
.r-ref {
  margin-left: auto;
  font-size: 0.74rem;
  color: var(--ow-cyan);
  opacity: 0.8;
  white-space: nowrap;
}

.r-body {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  padding: 0.8rem 0;
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 0.28rem;
}
.sec-label {
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  color: var(--ow-gold-bright);
}
.sec-text {
  margin: 0;
  font-size: 0.9rem;
  line-height: 1.7;
  color: var(--ow-ink);
}
.sec-rows {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.2rem 0.8rem;
}
.sec-row {
  display: contents;
}
.rk {
  color: var(--ow-muted);
  font-size: 0.82rem;
}
.rv {
  font-size: 0.88rem;
  line-height: 1.6;
}
.sec-list {
  margin: 0;
  padding-left: 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.22rem;
  font-size: 0.86rem;
  line-height: 1.6;
}
.sec-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}
.sec-chip {
  border: 1px solid var(--ow-line);
  /* FE-2: was 999px pill; micro-cut matches the cut-corner system */
  border-radius: 3px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
  background: rgba(16, 22, 48, 0.6);
  color: var(--ow-muted);
  font-size: 0.76rem;
  padding: 0.12rem 0.6rem;
}

.r-actions {
  display: flex;
  gap: 0.5rem;
  padding-top: 0.7rem;
  border-top: 1px solid var(--ow-line);
}
.revise-btn {
  margin-left: auto;
}

/* revise modal */
.rv-hint {
  margin: 0 0 0.8rem;
  font-size: 0.84rem;
  line-height: 1.6;
}
.rv-aspects {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.8rem;
}
.rv-aspect {
  border: 1px solid var(--ow-line);
  /* FE-2: was 999px pill; geometric toggle chip to match HSR language */
  border-radius: 3px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
  background: var(--ow-panel-2);
  color: var(--ow-muted);
  font-size: 0.82rem;
  padding: 0.3rem 0.8rem;
  cursor: pointer;
  transition: all 0.15s ease;
}
.rv-aspect.on {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.2);
}
.rv-note {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  /* FE-2: geometric to match panel language */
  border-radius: 2px;
  color: var(--ow-ink);
  padding: 0.6rem 0.7rem;
  font: inherit;
  font-size: 0.88rem;
  resize: vertical;
}

.queue {
  position: relative;
}
.card-enter-active,
.card-leave-active,
.card-move {
  transition: opacity 0.35s ease, transform 0.35s ease;
}
.card-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.card-leave-to {
  opacity: 0;
  transform: translateX(24px);
}
.card-leave-active {
  position: absolute;
  width: 100%;
}
@media (prefers-reduced-motion: reduce) {
  .card-enter-active,
  .card-leave-active,
  .card-move {
    transition: none;
  }
}

/* ---- 审阅上下文面板 ---- */
/* FE-4: was a bare panel with only border-top = looked like a plain form appended as a patch.
   Now adds glass background + inner top-glow to sit as a proper HSR secondary information layer. */
.ctx-panel {
  border-top: 1px solid var(--ow-gold-faint);
  margin-top: 0.1rem;
  overflow: hidden;
  background: rgba(10, 14, 36, 0.35);
  box-shadow: inset 0 1px 0 rgba(240, 210, 138, 0.07);
}

.ctx-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  background: transparent;
  border: none;
  border-radius: 0;
  color: var(--ow-ink-dim, var(--ow-muted));
  padding: 0.55rem 0;
  cursor: pointer;
  font-size: 0.82rem;
  font-family: inherit;
  text-align: left;
}

.ctx-toggle-label {
  font-family: var(--ow-overline);
  font-size: 0.66rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ow-violet);
}

.ctx-spinner {
  width: 14px;
  height: 14px;
}

.ctx-caret {
  margin-left: auto;
  color: var(--ow-muted);
  transition: transform 0.2s ease;
  font-size: 0.8rem;
}

.ctx-caret.open {
  transform: rotate(180deg);
}

.ctx-body {
  padding: 0.2rem 0.8rem 0.8rem;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.ctx-loading {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem 0;
  font-size: 0.82rem;
}

.ctx-loading .ow-spinner {
  width: 16px;
  height: 16px;
}

.ctx-err {
  font-size: 0.82rem;
  padding: 0.4rem 0;
}

/* context section: label + content.
   FE-4: sections separated by gold hairline dividers, matching the .section::after gold tail system.
   ✦ prefix on labels matches tokens.css .section::before { content:"✦" } convention. */
.ctx-section {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 0.65rem 0;
  border-bottom: 1px solid var(--ow-gold-faint);
}
.ctx-section:last-child {
  border-bottom: none;
}

.ctx-section-label {
  font-size: 0.68rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ow-gold-bright);
  font-family: var(--ow-overline);
}
.ctx-section-label::before {
  content: "✦ ";
  font-size: 0.55rem;
  opacity: 0.8;
  text-shadow: 0 0 6px rgba(240, 210, 138, 0.5);
}

/* field rows within a context section */
.ctx-field-row {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  margin: 0;
  font-size: 0.88rem;
  line-height: 1.6;
}

.ctx-k {
  font-size: 0.74rem;
  color: var(--ow-muted);
  white-space: nowrap;
  min-width: 4rem;
}

.ctx-v {
  color: var(--ow-ink);
}

/* stages block */
.ctx-stages {
  align-items: flex-start;
}

.ctx-stages-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  flex: 1;
}

.ctx-stage {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.84rem;
  line-height: 1.5;
}

.ctx-stage-id {
  font-size: 0.72rem;
  color: var(--ow-cyan);
  white-space: nowrap;
}

.ctx-stage-desc {
  color: var(--ow-ink);
}

/* issue chips */
.ctx-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}

.ctx-issue-chip {
  font-size: 0.72rem;
  padding: 0.15rem 0.5rem;
  border: 1px solid var(--ow-line);
  /* FE-2: geometric chip, not pill */
  border-radius: 3px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
  background: rgba(16, 22, 48, 0.6);
  color: var(--ow-cyan);
}

/* verdict row */
.ctx-verdict-row {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  flex-wrap: wrap;
}

.ctx-verdict {
  font-size: 0.84rem;
  font-weight: 600;
  padding: 0.2rem 0.65rem;
  /* FE-2: verdict badge — geometric cut instead of pill */
  border-radius: 3px;
  clip-path: polygon(4px 0, 100% 0, 100% calc(100% - 4px), calc(100% - 4px) 100%, 0 100%, 0 4px);
  border: 1px solid transparent;
}

.verdict-pass {
  border-color: rgba(100, 220, 150, 0.4);
  background: rgba(100, 220, 150, 0.1);
  color: #8de8b0;
}

.verdict-revise {
  border-color: rgba(224, 101, 58, 0.45);
  background: rgba(224, 101, 58, 0.1);
  color: #e0a878;
}

.verdict-null {
  border-color: var(--ow-line);
  background: transparent;
  color: var(--ow-muted);
  font-weight: 400;
}

.ctx-score {
  font-size: 0.9rem;
  color: var(--ow-gold);
  font-variant-numeric: tabular-nums;
}

.ctx-score small {
  font-size: 0.72rem;
  color: var(--ow-muted);
}

/* refine reflection */
.ctx-reflection {
  margin: 0;
  font-size: 0.86rem;
  line-height: 1.7;
  color: var(--ow-ink);
  border-left: 2px solid var(--ow-violet-soft);
  padding-left: 0.7rem;
}

.ctx-null-hint {
  margin: 0;
  font-size: 0.82rem;
}

/* calibration grid */
.ctx-calib-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem 1.6rem;
}

.ctx-calib-cell {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.ctx-calib-cell .ctx-k {
  font-size: 0.68rem;
  min-width: auto;
}

.ctx-calib-cell .ctx-v {
  font-size: 1rem;
}

.ctx-fp-warn {
  color: var(--ow-flag, #e0653a);
}
</style>
