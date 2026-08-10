<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError, apiGet, currentProject, humanizeError } from "../api";
import PageHead from "../components/PageHead.vue";
import AsyncState from "../components/AsyncState.vue";

interface Analytics {
  counts: Record<string, number>;
  entities_by_type: Record<string, number>;
  relation_density: number;
  faction_count: number;
  underdeveloped_faction_count: number;
  gap_counts: Record<string, number>;
  coverage: { kind: string; ready: number; total: number }[];
  overall_ready: number;
  overall_total: number;
}
interface FactionRow { id: string; name: string; members: number }
interface FactionPage {
  factions: FactionRow[];
  total: number;
  next_cursor: string | null;
}
interface GapPage {
  ids: string[];
  total: number;
  next_cursor: string | null;
}

const data = ref<Analytics | null>(null);
const loading = ref(true);
const error = ref("");
const factions = ref<FactionRow[]>([]);
const factionTotal = ref(0);
const factionNextCursor = ref<string | null>(null);
const factionsLoadingMore = ref(false);
const underdevelopedFactions = ref<FactionRow[]>([]);
const underdevelopedTotal = ref(0);
const underdevelopedNextCursor = ref<string | null>(null);
const underdevelopedLoadingMore = ref(false);
const expandedGap = ref("");
const gapIds = ref<Record<string, string[]>>({});
const gapTotals = ref<Record<string, number>>({});
const gapNextCursors = ref<Record<string, string | null>>({});
const gapLoading = ref("");
let loadSequence = 0;
let factionSequence = 0;
let underdevelopedSequence = 0;
let gapSequence = 0;

const COUNT_LABEL: Record<string, string> = {
  entities: "实体",
  quests: "任务",
  dialogues: "对话",
  dialogue_trees: "对话树",
  regions: "区域",
  pois: "地点",
  terms: "词条",
  relations: "关系",
  localized_texts: "本地化",
};
const GAP_LABEL: Record<string, string> = {
  entities_without_description: "实体缺简介",
  quests_without_objective: "任务缺目标",
  quests_without_stages: "任务缺阶段",
};

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  factionSequence += 1;
  underdevelopedSequence += 1;
  gapSequence += 1;
  loading.value = true;
  error.value = "";
  try {
    const project = currentProject();
    const [summary, factionPage, underdevelopedPage] = await Promise.all([
      apiGet<{ analytics: Analytics }>(`/projects/${project}/analytics/summary`),
      apiGet<FactionPage>(`/projects/${project}/analytics/factions?limit=30`),
      apiGet<FactionPage>(
        `/projects/${project}/analytics/factions?underdeveloped=true&limit=20`,
      ),
    ]);
    if (sequence !== loadSequence) return;
    data.value = summary.analytics;
    factions.value = factionPage.factions;
    factionTotal.value = factionPage.total;
    factionNextCursor.value = factionPage.next_cursor;
    underdevelopedFactions.value = underdevelopedPage.factions;
    underdevelopedTotal.value = underdevelopedPage.total;
    underdevelopedNextCursor.value = underdevelopedPage.next_cursor;
    expandedGap.value = "";
    gapIds.value = {};
    gapTotals.value = {};
    gapNextCursors.value = {};
  } catch (e) {
    if (sequence !== loadSequence) return;
    error.value = humanizeError(e);
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}
onMounted(load);

async function loadMoreFactions(underdeveloped: boolean): Promise<void> {
  const cursor = underdeveloped ? underdevelopedNextCursor.value : factionNextCursor.value;
  if (!cursor) return;
  const sequence = underdeveloped ? ++underdevelopedSequence : ++factionSequence;
  if (underdeveloped) underdevelopedLoadingMore.value = true;
  else factionsLoadingMore.value = true;
  try {
    const query = new URLSearchParams({ limit: underdeveloped ? "20" : "30", cursor });
    if (underdeveloped) query.set("underdeveloped", "true");
    const page = await apiGet<FactionPage>(
      `/projects/${currentProject()}/analytics/factions?${query.toString()}`,
    );
    const currentSequence = underdeveloped ? underdevelopedSequence : factionSequence;
    if (sequence !== currentSequence) return;
    if (underdeveloped) {
      underdevelopedFactions.value = [...underdevelopedFactions.value, ...page.factions];
      underdevelopedTotal.value = page.total;
      underdevelopedNextCursor.value = page.next_cursor;
    } else {
      factions.value = [...factions.value, ...page.factions];
      factionTotal.value = page.total;
      factionNextCursor.value = page.next_cursor;
    }
  } catch (caught) {
    const currentSequence = underdeveloped ? underdevelopedSequence : factionSequence;
    if (sequence !== currentSequence) return;
    if (caught instanceof ApiError && caught.status === 409) {
      await load();
      return;
    }
    error.value = humanizeError(caught);
  } finally {
    const currentSequence = underdeveloped ? underdevelopedSequence : factionSequence;
    if (sequence === currentSequence) {
      if (underdeveloped) underdevelopedLoadingMore.value = false;
      else factionsLoadingMore.value = false;
    }
  }
}

async function toggleGap(kind: string): Promise<void> {
  if (expandedGap.value === kind) {
    expandedGap.value = "";
    return;
  }
  expandedGap.value = kind;
  if (!(kind in gapIds.value)) await loadGap(kind);
}

async function loadGap(kind: string, append = false): Promise<void> {
  const cursor = append ? gapNextCursors.value[kind] : null;
  if (append && !cursor) return;
  const sequence = ++gapSequence;
  gapLoading.value = kind;
  try {
    const query = new URLSearchParams({ limit: "30" });
    if (cursor) query.set("cursor", cursor);
    const page = await apiGet<GapPage>(
      `/projects/${currentProject()}/analytics/gaps/${encodeURIComponent(kind)}?${query.toString()}`,
    );
    if (sequence !== gapSequence || expandedGap.value !== kind) return;
    gapIds.value = {
      ...gapIds.value,
      [kind]: append ? [...(gapIds.value[kind] ?? []), ...page.ids] : page.ids,
    };
    gapTotals.value = { ...gapTotals.value, [kind]: page.total };
    gapNextCursors.value = { ...gapNextCursors.value, [kind]: page.next_cursor };
  } catch (caught) {
    if (sequence !== gapSequence || expandedGap.value !== kind) return;
    if (append && caught instanceof ApiError && caught.status === 409) {
      await loadGap(kind);
      return;
    }
    error.value = humanizeError(caught);
  } finally {
    if (sequence === gapSequence) gapLoading.value = "";
  }
}

function gapObjectKind(kind: string): "entity" | "quest" {
  return kind.startsWith("entities_") ? "entity" : "quest";
}

function gapLocation(kind: string, id: string): string {
  const objectKind = gapObjectKind(kind);
  return `/archive?kind=${objectKind}&focus=${encodeURIComponent(`${objectKind}:${id}`)}&page=1`;
}

const coveragePct = computed(() => {
  const d = data.value;
  if (!d || !d.overall_total) return 0;
  return Math.round((d.overall_ready / d.overall_total) * 100);
});
</script>

<template>
  <section>
    <PageHead
      overline="ANALYTICS"
      title="世界分析"
      purpose="看清世界哪里厚、哪里薄：体量、关系密度、欠发育势力、待补缺口、就绪覆盖。"
    />
    <button class="ghost refresh" :disabled="loading" @click="load">
      {{ loading ? "刷新中…" : "刷新" }}
    </button>

    <AsyncState
      v-if="loading"
      state="loading"
      title="正在汇总世界数据"
      hint="体量、关系密度和就绪覆盖正在计算。"
    />
    <AsyncState
      v-else-if="error"
      state="error"
      title="世界分析读取失败"
      :hint="error"
      action-label="重新读取"
      @action="load"
    />
    <template v-else-if="data">
      <div class="cards">
        <div v-for="(v, k) in data.counts" :key="k" class="card">
          <span class="c-val">{{ v }}</span>
          <span class="c-lab">{{ COUNT_LABEL[k] ?? k }}</span>
        </div>
        <div class="card">
          <span class="c-val">{{ data.relation_density }}</span>
          <span class="c-lab">关系密度</span>
        </div>
        <div class="card">
          <span class="c-val">{{ coveragePct }}%</span>
          <span class="c-lab">就绪覆盖</span>
        </div>
      </div>

      <div v-if="underdevelopedTotal" class="sect">
        <h3>欠发育势力（无成员） · {{ underdevelopedTotal }}</h3>
        <span v-for="f in underdevelopedFactions" :key="f.id" class="chip">{{ f.name }}</span>
        <div class="page-meta">
          <span>已加载 {{ underdevelopedFactions.length }} / {{ underdevelopedTotal }}</span>
          <button
            v-if="underdevelopedNextCursor"
            class="ghost"
            :disabled="underdevelopedLoadingMore"
            @click="loadMoreFactions(true)"
          >{{ underdevelopedLoadingMore ? "读取中…" : "加载更多" }}</button>
        </div>
      </div>

      <div v-if="factionTotal" class="sect">
        <h3>势力成员数 · {{ factionTotal }}</h3>
        <div v-for="f in factions" :key="f.id" class="row">
          <span>{{ f.name }}</span><span class="muted mono">{{ f.members }} 名成员</span>
        </div>
        <div class="page-meta">
          <span>已加载 {{ factions.length }} / {{ factionTotal }}</span>
          <button
            v-if="factionNextCursor"
            class="ghost"
            :disabled="factionsLoadingMore"
            @click="loadMoreFactions(false)"
          >{{ factionsLoadingMore ? "读取中…" : "加载更多势力" }}</button>
        </div>
      </div>

      <div class="sect">
        <h3>待补缺口</h3>
        <div v-for="(count, k) in data.gap_counts" :key="k" class="gap-block">
          <button class="row gap-toggle" type="button" @click="toggleGap(String(k))">
            <span>{{ GAP_LABEL[k] ?? k }}</span>
            <span class="muted mono">{{ count }} 项 · {{ expandedGap === k ? "收起" : "查看对象" }}</span>
          </button>
          <div v-if="expandedGap === k" class="gap-ids">
            <span v-if="gapLoading === k && !(k in gapIds)" class="muted">读取中…</span>
            <RouterLink
              v-for="id in gapIds[k] ?? []"
              :key="id"
              class="gap-id mono"
              :to="gapLocation(String(k), id)"
            >{{ id }}</RouterLink>
            <div v-if="gapIds[k]" class="page-meta">
              <span>已加载 {{ gapIds[k]?.length ?? 0 }} / {{ gapTotals[k] ?? count }}</span>
              <button
                v-if="gapNextCursors[k]"
                class="ghost"
                :disabled="gapLoading === k"
                @click="loadGap(String(k), true)"
              >{{ gapLoading === k ? "读取中…" : "加载更多对象" }}</button>
            </div>
          </div>
        </div>
      </div>
    </template>
    <AsyncState
      v-else
      state="empty"
      title="还没有可分析的数据"
      hint="先创建或打开一个世界，并录入一些设定。"
    />
  </section>
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 96px), 1fr));
  gap: 10px;
  margin: 1rem 0 1.4rem;
}
.card {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  padding: 0.8rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  align-items: center;
}
.c-val {
  font-size: 1.4rem;
  color: var(--ow-gold, #d8b46a);
}
.c-lab {
  font-size: 0.74rem;
  color: var(--ow-muted);
}
.sect {
  margin: 1.2rem 0;
}
.sect h3 {
  font-size: 0.95rem;
  margin: 0 0 0.5rem;
}
.row {
  display: flex;
  justify-content: space-between;
  padding: 0.35rem 0;
  border-top: 1px solid var(--ow-line);
  font-size: 0.85rem;
}
.page-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
  margin-top: 0.55rem;
  color: var(--ow-muted);
  font-size: 0.74rem;
}
.page-meta button { min-height: 34px; }
.gap-block { border-top: 1px solid var(--ow-line); }
.gap-toggle {
  width: 100%;
  border: 0;
  background: transparent;
  color: var(--ow-ink);
  cursor: pointer;
  text-align: left;
}
.gap-ids { display: flex; flex-wrap: wrap; gap: 0.35rem; padding: 0.35rem 0 0.7rem; }
.gap-id {
  padding: 0.2rem 0.45rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.35rem;
  color: var(--ow-cyan);
  font-size: 0.72rem;
  text-decoration: none;
}
.gap-ids .page-meta { width: 100%; }
.chip {
  display: inline-block;
  font-size: 0.78rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  padding: 0.2rem 0.55rem;
  margin: 0 0.35rem 0.35rem 0;
  color: var(--ow-flag, #e0653a);
}
.empty {
  padding: 2rem 0;
}

.refresh {
  min-height: 40px;
}

.refresh:disabled {
  cursor: wait;
  opacity: 0.62;
}

@media (max-width: 520px) {
  .refresh {
    min-height: 44px;
    width: 100%;
  }

  .row {
    align-items: flex-start;
    flex-direction: column;
    gap: 0.2rem;
  }
}
</style>
