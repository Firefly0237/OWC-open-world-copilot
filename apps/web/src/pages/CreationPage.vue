<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  ApiError,
  humanizeError,
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentProject,
  llmConfig,
  llmParams,
} from "../api";
import { example } from "../examples";
import EmptyState from "../components/EmptyState.vue";
import PageHead from "../components/PageHead.vue";

const phQuestBrief = example("questBrief");
const phDialogue = example("dialogueBrief");

type Tab = "draft" | "dialogue" | "barks" | "flavor";
const TABS: { key: Tab; label: string }[] = [
  { key: "draft", label: "任务草稿" },
  { key: "dialogue", label: "对话树" },
  { key: "barks", label: "台词" },
  { key: "flavor", label: "物案" },
];

interface ArchiveEntity {
  id: string;
  name: string;
  type: string;
}

interface ReferenceReportRow {
  source_ref: string;
  source_title: string;
  used_for: string;
  transformation: string;
  leakage?: { checked?: boolean; detected?: boolean; message?: string } | null;
}

const tab = ref<Tab>("draft");
const npcs = ref<ArchiveEntity[]>([]);
const npcTotal = ref(0);
const npcScanned = ref(0);
const npcNextCursor = ref<string | null>(null);
const npcLoading = ref(false);
const npcError = ref("");
const NPC_PAGE_SIZE = 40;
let npcLoadSequence = 0;
const error = ref("");
const llmReady = ref(llmConfig().ready);

// shared per-tab busy + result note + cost
const busy = reactive<Record<Tab, boolean>>({ draft: false, dialogue: false, barks: false, flavor: false });
const lastCost = reactive<Record<Tab, number>>({ draft: 0, dialogue: 0, barks: 0, flavor: 0 });

// draft
const draftBrief = ref("");
const draftUseReferences = ref(false);
const draftRefineRounds = ref(2);
type RefineRound = { round: number; verdict: string; score: number; readiness_score: number; new_error_count: number; fixes: string[]; auto_review_ok: boolean };
const draftResult = ref<{
  title: string;
  objective: string;
  stages: string[];
  issues: number;
  refineTrail: RefineRound[];
  autoReviewIncomplete: boolean;
  inspirationRefs: string[];
  referenceReport: ReferenceReportRow[];
} | null>(null);

// dialogue
const dlgParticipants = ref<string[]>([]);
const dlgBrief = ref("");
const dlgQuestId = ref("");
const dlgMaxNodes = ref(12);
const dlgMaxChars = ref(120);
const dlgUseReferences = ref(false);
const dlgRefineRounds = ref(1);
const dlgResult = ref<{
  nodes: number;
  lint: number;
  problems: number;
  autoReviewIncomplete: boolean;
  inspirationRefs: string[];
  referenceReport: ReferenceReportRow[];
} | null>(null);

// barks
const barkSpeakers = ref<string[]>([]);
const barkTopic = ref("");
const barkVariants = ref(3);
const barkMaxChars = ref(40);
const barkAllowedEntityIds = ref("");
const barkUseReferences = ref(false);
const barkRefineRounds = ref(0);
const barkResult = ref<{
  accepted: { speaker_id: string; text: string }[];
  rejected: number;
  inspirationRefs: string[];
  referenceReport: ReferenceReportRow[];
} | null>(null);

// flavor
const flavorCategory = ref<"item" | "skill" | "achievement">("item");
const flavorNames = ref("");
const flavorTheme = ref("");
const flavorMaxChars = ref(120);
const flavorUseReferences = ref(false);
const flavorRefineRounds = ref(0);
const flavorResult = ref<{
  accepted: { name: string; description?: string }[];
  rejected: number;
  inspirationRefs: string[];
  referenceReport: ReferenceReportRow[];
} | null>(null);

const FLAVOR_LABEL = { item: "物品", skill: "技能", achievement: "成就" };

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}

interface ArchiveEntityPage {
  items: ArchiveEntity[];
  total: number;
  next_cursor: string | null;
}

async function loadNpcs(cursor = "", append = false): Promise<void> {
  if (append && npcLoading.value) return;
  const sequence = ++npcLoadSequence;
  npcLoading.value = true;
  if (!append) npcError.value = "";
  try {
    const params = new URLSearchParams({ kind: "entity", limit: String(NPC_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    const body = await apiGet<ArchiveEntityPage>(
      `/projects/${currentProject()}/archive?${params.toString()}`,
    );
    if (sequence !== npcLoadSequence) return;
    const pageNpcs = body.items.filter((entity) => entity.type === "npc");
    const rows = append ? [...npcs.value, ...pageNpcs] : pageNpcs;
    npcs.value = [...new Map(rows.map((entity) => [entity.id, entity])).values()];
    npcTotal.value = body.total;
    npcScanned.value = append ? npcScanned.value + body.items.length : body.items.length;
    npcNextCursor.value = body.next_cursor;
  } catch (e) {
    if (sequence !== npcLoadSequence) return;
    if (cursor && e instanceof ApiError && e.status === 409) {
      await loadNpcs();
      return;
    }
    npcError.value = humanizeError(e);
  } finally {
    if (sequence === npcLoadSequence) npcLoading.value = false;
  }
}

async function loadMoreNpcs(): Promise<void> {
  if (!npcNextCursor.value) return;
  await loadNpcs(npcNextCursor.value, true);
}

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  await loadNpcs();
});
onUnmounted(() => {
  npcLoadSequence += 1;
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});

function nameOf(id: string): string {
  return npcs.value.find((n) => n.id === id)?.name ?? id;
}

// In the template a ref auto-unwraps, so the handler passes the reactive array itself (not the
// ref). Mutating it in place (splice/push) is what Vue tracks — and what actually works at runtime.
function toggle(list: string[], id: string): void {
  const i = list.indexOf(id);
  if (i >= 0) list.splice(i, 1);
  else list.push(id);
}

function parseIdList(source: string): string[] {
  return [...new Set(source.split(/[\s,，]+/).map((value) => value.trim()).filter(Boolean))];
}

async function call<T>(key: Tab, path: string, body: unknown): Promise<T | null> {
  busy[key] = true;
  error.value = "";
  try {
    const res = await apiPost<T & { cost_budget?: { used_usd?: number } }>(path, body);
    const used = costOf(res);
    lastCost[key] = used;
    addSessionCost(used);
    return res;
  } catch (e) {
    error.value = humanizeError(e);
    return null;
  } finally {
    busy[key] = false;
  }
}

async function runDraft(): Promise<void> {
  if (!draftBrief.value.trim() || !llmReady.value) return;
  draftResult.value = null;
  const r = await call<{ quest: { title: string; objective: string; stages?: { summary: string }[] }; issues: unknown[]; refine_trail?: RefineRound[]; auto_review_incomplete?: boolean; inspiration_refs?: string[]; reference_report?: ReferenceReportRow[] }>(
    "draft",
    `/projects/${currentProject()}/contents/quests:draft`,
    {
      brief: draftBrief.value.trim(),
      refine_rounds: draftRefineRounds.value,
      use_references: draftUseReferences.value,
      ...llmParams(),
    },
  );
  if (r)
    draftResult.value = {
      title: r.quest.title,
      objective: r.quest.objective,
      stages: (r.quest.stages ?? []).map((s) => s.summary),
      issues: r.issues.length,
      refineTrail: r.refine_trail ?? [],
      autoReviewIncomplete: r.auto_review_incomplete ?? false,
      inspirationRefs: r.inspiration_refs ?? [],
      referenceReport: r.reference_report ?? [],
    };
}

async function runDialogue(): Promise<void> {
  if (dlgParticipants.value.length === 0 || !dlgBrief.value.trim() || !llmReady.value) return;
  dlgResult.value = null;
  const r = await call<{
    tree: { nodes?: unknown[] };
    lint_issues: unknown[];
    structure_problems: unknown[];
    auto_review_incomplete?: boolean;
    inspiration_refs?: string[];
    reference_report?: ReferenceReportRow[];
  }>("dialogue", `/projects/${currentProject()}/assist/dialogue_trees:draft`, {
    participant_ids: dlgParticipants.value,
    brief: dlgBrief.value.trim(),
    quest_id: dlgQuestId.value.trim() || null,
    max_nodes: dlgMaxNodes.value,
    max_chars: dlgMaxChars.value,
    refine_rounds: dlgRefineRounds.value,
    use_references: dlgUseReferences.value,
    ...llmParams(),
  });
  if (r)
    dlgResult.value = {
      nodes: (r.tree.nodes ?? []).length,
      lint: r.lint_issues.length,
      problems: r.structure_problems.length,
      autoReviewIncomplete: r.auto_review_incomplete ?? false,
      inspirationRefs: r.inspiration_refs ?? [],
      referenceReport: r.reference_report ?? [],
    };
}

async function runBarks(): Promise<void> {
  if (barkSpeakers.value.length === 0 || !barkTopic.value.trim() || !llmReady.value) return;
  barkResult.value = null;
  const r = await call<{ accepted: { speaker_id: string; text: string }[]; rejected: unknown[]; inspiration_refs?: string[]; reference_report?: ReferenceReportRow[] }>(
    "barks",
    `/projects/${currentProject()}/assist/barks:batch`,
    {
      speaker_ids: barkSpeakers.value,
      topic: barkTopic.value.trim(),
      variants_per_speaker: barkVariants.value,
      max_chars: barkMaxChars.value,
      allowed_entity_ids: parseIdList(barkAllowedEntityIds.value),
      refine_rounds: barkRefineRounds.value,
      use_references: barkUseReferences.value,
      ...llmParams(),
    },
  );
  if (r)
    barkResult.value = {
      accepted: r.accepted,
      rejected: r.rejected.length,
      inspirationRefs: r.inspiration_refs ?? [],
      referenceReport: r.reference_report ?? [],
    };
}

async function runFlavor(): Promise<void> {
  const names = flavorNames.value
    .split(/[\n,，]/)
    .map((n) => n.trim())
    .filter(Boolean);
  if (names.length === 0 || !llmReady.value) return;
  flavorResult.value = null;
  const r = await call<{ accepted: { name: string; description?: string }[]; rejected: unknown[]; inspiration_refs?: string[]; reference_report?: ReferenceReportRow[] }>(
    "flavor",
    `/projects/${currentProject()}/assist/flavor:batch`,
    {
      category: flavorCategory.value,
      names,
      theme: flavorTheme.value.trim(),
      max_chars: flavorMaxChars.value,
      refine_rounds: flavorRefineRounds.value,
      use_references: flavorUseReferences.value,
      ...llmParams(),
    },
  );
  if (r)
    flavorResult.value = {
      accepted: r.accepted,
      rejected: r.rejected.length,
      inspirationRefs: r.inspiration_refs ?? [],
      referenceReport: r.reference_report ?? [],
    };
}

const flavorNameCount = computed(
  () => flavorNames.value.split(/[\n,，]/).filter((n) => n.trim()).length,
);
</script>

<template>
  <section>
    <PageHead overline="CREATION" title="创作工坊" purpose="生成任务、对话、台词或物案。" />

    <div class="tabs">
      <button v-for="t in TABS" :key="t.key" class="tab" :class="{ on: tab === t.key }" @click="tab = t.key">
        {{ t.label }}
      </button>
    </div>

    <p v-if="!llmReady" class="muted notice">
      请先在 <RouterLink to="/settings" class="golink">设置</RouterLink> 里接入模型。
    </p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="npcError" class="error">读取角色档案失败：{{ npcError }}</p>
    <div class="npc-pager pane">
      <span class="muted small">
        角色选择器已从 {{ npcScanned }} / {{ npcTotal }} 条实体中载入 {{ npcs.length }} 位角色。
      </span>
      <button v-if="npcNextCursor" class="ghost" :disabled="npcLoading" @click="loadMoreNpcs">
        {{ npcLoading ? "载入中…" : "加载下一页角色" }}
      </button>
    </div>

    <!-- 任务草稿 -->
    <div v-show="tab === 'draft'" class="workspace">
      <div class="io pane form">
        <label class="field">
          <span class="label">任务简述</span>
          <textarea v-model="draftBrief" rows="4" maxlength="4000" :placeholder="`例如：${phQuestBrief}`"></textarea>
        </label>
        <label class="reference-option">
          <input v-model="draftUseReferences" type="checkbox" />
          <span>从允许用于草拟的灵感素材中检索参考，并保留溯源</span>
        </label>
        <label class="field inline">
          <span class="label">自评精修 {{ draftRefineRounds }} 轮</span>
          <input v-model.number="draftRefineRounds" type="range" min="0" max="4" />
        </label>
        <button class="primary" :disabled="busy.draft || !draftBrief.trim() || !llmReady" @click="runDraft">
          {{ busy.draft ? "起草中…" : "生成任务草稿" }}
        </button>
      </div>
      <div class="out">
        <div v-if="draftResult" class="result pane reveal">
          <div class="r-head"><b>{{ draftResult.title }}</b><span v-if="lastCost.draft" class="cost">${{ lastCost.draft.toFixed(4) }}</span></div>
          <p class="objective">{{ draftResult.objective }}</p>
          <ol v-if="draftResult.stages.length" class="stages">
            <li v-for="(s, i) in draftResult.stages" :key="i">{{ s }}</li>
          </ol>
          <p v-if="draftResult.autoReviewIncomplete" class="auto-review-warn">
            自动评审未能完成，这份草稿没有通过质量自检——请在审阅时重点复核。
          </p>
          <div v-if="draftResult.refineTrail.length" class="refine">
            <div class="refine-head">
              <span class="refine-title">自评精修 · {{ draftResult.refineTrail.length }} 轮</span>
              <span class="refine-note">已自动评审并改写</span>
            </div>
            <ol class="refine-rounds reveal">
              <li v-for="rd in draftResult.refineTrail" :key="rd.round" class="refine-round">
                <span class="badge" :class="rd.verdict === 'pass' ? 'ok' : 'revise'">
                  第 {{ rd.round + 1 }} 轮 · {{ rd.verdict === "pass" ? "通过" : "需精修" }}
                </span>
                <span class="metric">就绪度 {{ Math.round(rd.readiness_score * 100) }}%</span>
                <span class="metric" v-if="rd.new_error_count">新增错误 {{ rd.new_error_count }}</span>
                <ul v-if="rd.verdict !== 'pass' && rd.fixes.length" class="fixes">
                  <li v-for="(f, i) in rd.fixes.slice(0, 4)" :key="i">{{ f }}</li>
                </ul>
                <span v-if="rd.verdict !== 'pass' && rd.fixes.length > 4" class="muted small">
                  还有 {{ rd.fixes.length - 4 }} 项建议未展开
                </span>
              </li>
            </ol>
          </div>
          <p class="muted small">生成时发现 {{ draftResult.issues }} 处待留意。</p>
          <details v-if="draftUseReferences" class="reference-report">
            <summary>灵感溯源 · {{ draftResult.inspirationRefs.length }} 条引用</summary>
            <p v-if="!draftResult.referenceReport.length" class="muted small">本次没有召回可用于草拟的素材。</p>
            <ul v-else>
              <li v-for="row in draftResult.referenceReport" :key="row.source_ref">
                <b>{{ row.source_title }}</b> · {{ row.used_for }}
                <span class="muted">{{ row.transformation }}</span>
                <span v-if="row.leakage?.message" :class="{ warn: row.leakage.detected }">{{ row.leakage.message }}</span>
              </li>
            </ul>
          </details>
        </div>
        <EmptyState
          v-else
          :busy="busy.draft"
          :title="busy.draft ? '起草中' : '任务草稿'"
          :hint="busy.draft ? '接地设定 · 自评精修' : '写一句简述，点生成'"
        />
      </div>
    </div>

    <!-- 对话树 -->
    <div v-show="tab === 'dialogue'" class="workspace">
      <div class="io pane form">
        <div class="field">
          <span class="label">参与角色（点选，至少 1 个）</span>
          <div class="chips">
            <button v-for="n in npcs" :key="n.id" type="button" class="chip" :class="{ on: dlgParticipants.includes(n.id) }" @click="toggle(dlgParticipants, n.id)">{{ n.name }}</button>
            <span v-if="!npcs.length" class="muted small">
              {{ npcNextCursor ? "当前页还没有角色，可继续加载。" : "这个世界还没有角色，先去人物工坊或创世生成。" }}
            </span>
          </div>
        </div>
        <label class="field">
          <span class="label">情境简述</span>
          <textarea v-model="dlgBrief" rows="3" maxlength="2000" :placeholder="`例如：${phDialogue}`"></textarea>
        </label>
        <label class="field">
          <span class="label">关联任务 ID <i class="muted">可选；用于让对话节点接入任务</i></span>
          <input v-model="dlgQuestId" maxlength="200" placeholder="例如：quest_mist_harbor" />
        </label>
        <div class="limit-grid">
          <label class="field">
            <span class="label">最多节点（4–24）</span>
            <input v-model.number="dlgMaxNodes" type="number" min="4" max="24" />
          </label>
          <label class="field">
            <span class="label">单句最多字符（20–400）</span>
            <input v-model.number="dlgMaxChars" type="number" min="20" max="400" />
          </label>
        </div>
        <label class="reference-option">
          <input v-model="dlgUseReferences" type="checkbox" />
          <span>从允许用于草拟的灵感素材中检索参考，并保留溯源</span>
        </label>
        <label class="field inline">
          <span class="label">自评精修 {{ dlgRefineRounds }} 轮</span>
          <input v-model.number="dlgRefineRounds" type="range" min="0" max="4" />
        </label>
        <button class="primary" :disabled="busy.dialogue || !dlgParticipants.length || !dlgBrief.trim() || !llmReady" @click="runDialogue">
          {{ busy.dialogue ? "推演中…" : "生成对话树" }}
        </button>
      </div>
      <div class="out">
        <div v-if="dlgResult" class="result pane reveal">
          <div class="r-head"><b>对话树已生成</b><span v-if="lastCost.dialogue" class="cost">${{ lastCost.dialogue.toFixed(4) }}</span></div>
          <div class="chips static">
            <span class="chip">{{ dlgResult.nodes }} 节点</span>
            <span class="chip" :class="{ amber: dlgResult.lint }">{{ dlgResult.lint }} 条待复核</span>
            <span class="chip" :class="{ red: dlgResult.problems }">{{ dlgResult.problems }} 结构问题</span>
          </div>
          <p v-if="dlgResult.autoReviewIncomplete" class="auto-review-warn">
            自动评审未能完成，这棵对话树没有通过质量自检——请在审阅时重点复核。
          </p>
          <details v-if="dlgUseReferences" class="reference-report">
            <summary>灵感溯源 · {{ dlgResult.inspirationRefs.length }} 条引用</summary>
            <p v-if="!dlgResult.referenceReport.length" class="muted small">本次没有召回可用于草拟的素材。</p>
            <ul v-else>
              <li v-for="row in dlgResult.referenceReport" :key="row.source_ref">
                <b>{{ row.source_title }}</b> · {{ row.used_for }}
                <span class="muted">{{ row.transformation }}</span>
                <span v-if="row.leakage?.message" :class="{ warn: row.leakage.detected }">{{ row.leakage.message }}</span>
              </li>
            </ul>
          </details>
        </div>
        <EmptyState
          v-else
          :busy="busy.dialogue"
          :title="busy.dialogue ? '推演中' : '对话树'"
          :hint="busy.dialogue ? '' : '选角色、写情境，点生成'"
        />
      </div>
    </div>

    <!-- 台词 -->
    <div v-show="tab === 'barks'" class="workspace">
      <div class="io pane form">
        <div class="field">
          <span class="label">说话人（点选，至少 1 个）</span>
          <div class="chips">
            <button v-for="n in npcs" :key="n.id" type="button" class="chip" :class="{ on: barkSpeakers.includes(n.id) }" @click="toggle(barkSpeakers, n.id)">{{ n.name }}</button>
            <span v-if="!npcs.length" class="muted small">
              {{ npcNextCursor ? "当前页还没有角色，可继续加载。" : "这个世界还没有角色。" }}
            </span>
          </div>
        </div>
        <label class="field">
          <span class="label">话题 / 场合</span>
          <input v-model="barkTopic" maxlength="1000" placeholder="例如：发现可疑船影时的警戒喊话" />
        </label>
        <label class="field inline">
          <span class="label">每人变体数 {{ barkVariants }}</span>
          <input v-model.number="barkVariants" type="range" min="1" max="8" />
        </label>
        <label class="field">
          <span class="label">单句最多字符（8–500）</span>
          <input v-model.number="barkMaxChars" type="number" min="8" max="500" />
        </label>
        <label class="field">
          <span class="label">额外允许引用的实体 ID <i class="muted">可选；空格、逗号或换行分隔</i></span>
          <textarea
            v-model="barkAllowedEntityIds"
            rows="2"
            maxlength="4000"
            placeholder="例如：loc_mist_harbor, item_signal_bell"
          ></textarea>
        </label>
        <label class="reference-option">
          <input v-model="barkUseReferences" type="checkbox" />
          <span>从允许用于草拟的灵感素材中检索参考，并保留溯源</span>
        </label>
        <label class="field inline">
          <span class="label">自评精修 {{ barkRefineRounds }} 轮</span>
          <input v-model.number="barkRefineRounds" type="range" min="0" max="4" />
        </label>
        <button class="primary" :disabled="busy.barks || !barkSpeakers.length || !barkTopic.trim() || !llmReady" @click="runBarks">
          {{ busy.barks ? "生成中…" : "生成台词" }}
        </button>
      </div>
      <div class="out">
        <div v-if="barkResult" class="result pane reveal">
          <div class="r-head"><b>采纳 {{ barkResult.accepted.length }} 条</b><span v-if="lastCost.barks" class="cost">${{ lastCost.barks.toFixed(4) }}</span></div>
          <div class="lines">
            <div v-for="(v, i) in barkResult.accepted" :key="i" class="line"><span class="who">{{ nameOf(v.speaker_id) }}</span>{{ v.text }}</div>
          </div>
          <p v-if="barkResult.rejected" class="muted small">{{ barkResult.rejected }} 条因引用越界被拦下。</p>
          <details v-if="barkUseReferences" class="reference-report">
            <summary>灵感溯源 · {{ barkResult.inspirationRefs.length }} 条引用</summary>
            <p v-if="!barkResult.referenceReport.length" class="muted small">本次没有召回可用于草拟的素材。</p>
            <ul v-else>
              <li v-for="row in barkResult.referenceReport" :key="row.source_ref">
                <b>{{ row.source_title }}</b> · {{ row.used_for }}
                <span class="muted">{{ row.transformation }}</span>
                <span v-if="row.leakage?.message" :class="{ warn: row.leakage.detected }">{{ row.leakage.message }}</span>
              </li>
            </ul>
          </details>
        </div>
        <EmptyState
          v-else
          :busy="busy.barks"
          :title="busy.barks ? '生成中' : '角色台词'"
          :hint="busy.barks ? '' : '选说话人、写话题，点生成'"
        />
      </div>
    </div>

    <!-- 物案 -->
    <div v-show="tab === 'flavor'" class="workspace">
      <div class="io pane form">
        <div class="field inline">
          <span class="label">类别</span>
          <select v-model="flavorCategory">
            <option value="item">物品</option>
            <option value="skill">技能</option>
            <option value="achievement">成就</option>
          </select>
        </div>
        <label class="field">
          <span class="label">名称（每行或逗号一个，最多 50 个）<i class="muted">{{ flavorNameCount }} 个</i></span>
          <textarea v-model="flavorNames" rows="4" placeholder="雾铃灯&#10;盐渍罗盘&#10;回音封蜡"></textarea>
        </label>
        <label class="field">
          <span class="label">风格基调（可选）</span>
          <input v-model="flavorTheme" maxlength="200" placeholder="例如：潮湿、年久、带咸味的工艺感" />
        </label>
        <label class="field">
          <span class="label">每条最多字符（20–400）</span>
          <input v-model.number="flavorMaxChars" type="number" min="20" max="400" />
        </label>
        <label class="reference-option">
          <input v-model="flavorUseReferences" type="checkbox" />
          <span>从允许用于草拟的灵感素材中检索参考，并保留溯源</span>
        </label>
        <label class="field inline">
          <span class="label">自评精修 {{ flavorRefineRounds }} 轮</span>
          <input v-model.number="flavorRefineRounds" type="range" min="0" max="4" />
        </label>
        <button class="primary" :disabled="busy.flavor || !flavorNameCount || !llmReady" @click="runFlavor">
          {{ busy.flavor ? "撰写中…" : `生成${FLAVOR_LABEL[flavorCategory]}文案` }}
        </button>
      </div>
      <div class="out">
        <div v-if="flavorResult" class="result pane reveal">
          <div class="r-head"><b>采纳 {{ flavorResult.accepted.length }} 条</b><span v-if="lastCost.flavor" class="cost">${{ lastCost.flavor.toFixed(4) }}</span></div>
          <div class="cards">
            <div v-for="(e, i) in flavorResult.accepted" :key="i" class="fcard"><b>{{ e.name }}</b><span class="muted">{{ e.description }}</span></div>
          </div>
          <p v-if="flavorResult.rejected" class="muted small">{{ flavorResult.rejected }} 条因引用越界被拦下。</p>
          <details v-if="flavorUseReferences" class="reference-report">
            <summary>灵感溯源 · {{ flavorResult.inspirationRefs.length }} 条引用</summary>
            <p v-if="!flavorResult.referenceReport.length" class="muted small">本次没有召回可用于草拟的素材。</p>
            <ul v-else>
              <li v-for="row in flavorResult.referenceReport" :key="row.source_ref">
                <b>{{ row.source_title }}</b> · {{ row.used_for }}
                <span class="muted">{{ row.transformation }}</span>
                <span v-if="row.leakage?.message" :class="{ warn: row.leakage.detected }">{{ row.leakage.message }}</span>
              </li>
            </ul>
          </details>
        </div>
        <EmptyState
          v-else
          :busy="busy.flavor"
          :title="busy.flavor ? '撰写中' : '物品文案'"
          :hint="busy.flavor ? '' : '填名称、定基调，点生成'"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
/* two-zone workspace: input on the left, output on the right. The output zone is ALWAYS present —
   standing-by when idle, busy while running, results when done — so the page never reads as empty. */
.workspace {
  display: grid;
  grid-template-columns: minmax(0, 5fr) minmax(0, 6fr);
  gap: clamp(0.8rem, 1.4vw, 1.4rem);
  align-items: start;
}
.io {
  position: sticky;
  top: 1.2rem;
}
.out {
  min-width: 0;
}
.out .result {
  padding: 1.1rem 1.2rem;
}
@media (max-width: 920px) {
  .workspace {
    grid-template-columns: 1fr;
  }
  .io {
    position: static;
  }
}

.tabs {
  display: flex;
  gap: 0.4rem;
  margin-bottom: 0.8rem;
  flex-wrap: wrap;
}

.tab {
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius) var(--ow-control-radius) 0 0;
  background: transparent;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.86rem;
  padding: 0.4rem 0.9rem;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}

.tab.on {
  color: var(--ow-gold-bright);
  border-bottom-color: var(--ow-gold);
}

.notice,
.error {
  font-size: 0.85rem;
}

.notice {
  color: var(--ow-muted);
}

.npc-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
  margin-bottom: 0.8rem;
  padding: 0.55rem 0.75rem;
}

.npc-pager .ghost {
  min-height: 36px;
  white-space: nowrap;
}

.error {
  color: #e89a9a;
}

.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field.inline {
  flex-direction: row;
  align-items: center;
  gap: 0.7rem;
}

.limit-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
}

@media (max-width: 560px) {
  .limit-grid {
    grid-template-columns: 1fr;
  }
}

.reference-option {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  min-height: 44px;
  color: var(--ow-muted);
  font-size: 0.8rem;
}

.reference-option input {
  width: 1rem;
  height: 1rem;
  padding: 0;
  accent-color: var(--ow-gold);
}

.reference-report {
  margin-top: 0.6rem;
  padding: 0.5rem 0.65rem;
  border: 1px solid var(--ow-line);
  border-radius: 8px;
  background: var(--ow-panel-2);
  font-size: 0.78rem;
}

.reference-report summary {
  color: var(--ow-gold-bright);
  cursor: pointer;
}

.reference-report ul {
  margin: 0.5rem 0 0;
  padding-left: 1.1rem;
}

.reference-report li,
.reference-report li span {
  display: block;
  margin-top: 0.2rem;
}

.reference-report .warn {
  color: #e0a878;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

.label i {
  font-style: normal;
  margin-left: 0.4rem;
  font-size: 0.74rem;
}

textarea,
input,
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

textarea {
  resize: vertical;
}

textarea:focus,
input:focus,
select:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
}

.chip {
  border: 1px solid var(--ow-line);
  background: rgba(16, 22, 48, 0.6);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.8rem;
  padding: 0.22rem 0.7rem;
  cursor: pointer;
}

.chip.on {
  border-color: var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
}

.chips.static .chip {
  cursor: default;
}

.chip.amber {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.chip.red {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
  color: #241a05;
  font-weight: 600;
  padding: 0.55rem 1rem;
  cursor: pointer;
  align-self: flex-start;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.result {
  border-top: 1px solid var(--ow-line);
  padding-top: 0.7rem;
  margin-top: 0.2rem;
}

.r-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  margin-bottom: 0.3rem;
}

.r-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  color: var(--ow-cyan);
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.1rem 0.5rem;
}

.objective {
  margin: 0 0 0.4rem;
  line-height: 1.6;
}

.stages {
  margin: 0 0 0.4rem;
  padding-left: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.86rem;
}

.small {
  font-size: 0.78rem;
}

.auto-review-warn {
  margin: 0.5rem 0;
  padding: 0.5rem 0.7rem;
  border: 1px solid color-mix(in srgb, #d98a5a 55%, var(--ow-line));
  border-radius: 8px;
  background: color-mix(in srgb, #d98a5a 12%, transparent);
  color: #e0a878;
  font-size: 0.82rem;
}

.refine {
  margin: 0.5rem 0 0.6rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid var(--ow-line);
  border-radius: 8px;
  background: var(--ow-panel-2);
}
.refine-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
  margin-bottom: 0.45rem;
}
.refine-title {
  color: var(--ow-gold-bright);
  font-size: 0.82rem;
  font-weight: 600;
}
.refine-note {
  color: var(--ow-muted);
  font-size: 0.72rem;
}
.refine-rounds {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.refine-round {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem 0.55rem;
}
.refine-round .badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.5rem;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  border: 1px solid var(--ow-line);
}
.badge.ok {
  color: var(--ow-cyan);
  border-color: var(--ow-cyan);
  background: color-mix(in srgb, var(--ow-cyan) 12%, transparent);
}
.badge.revise {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
  background: var(--ow-gold-faint);
}
.refine-round .metric {
  font-size: 0.74rem;
  color: var(--ow-muted);
}
.fixes {
  flex-basis: 100%;
  margin: 0.1rem 0 0;
  padding-left: 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  font-size: 0.76rem;
  color: var(--ow-muted);
}

.lines {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.4rem;
}

.line {
  font-size: 0.86rem;
  line-height: 1.5;
}

.who {
  color: var(--ow-gold-bright);
  margin-right: 0.5rem;
  font-size: 0.8rem;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.fcard {
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.5rem 0.7rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.84rem;
}

.fcard b {
  color: var(--ow-gold-bright);
}
</style>
