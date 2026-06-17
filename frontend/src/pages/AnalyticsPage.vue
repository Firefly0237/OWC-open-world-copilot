<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, currentProject } from "../api";
import { notifyError } from "../toast";
import PageHead from "../components/PageHead.vue";

interface Analytics {
  counts: Record<string, number>;
  entities_by_type: Record<string, number>;
  relation_density: number;
  factions: { id: string; name: string; members: number }[];
  underdeveloped_factions: { id: string; name: string }[];
  gaps: Record<string, string[]>;
  coverage: { kind: string; ready: number; total: number }[];
  overall_ready: number;
  overall_total: number;
}

const data = ref<Analytics | null>(null);

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
  try {
    data.value = (await apiGet<{ analytics: Analytics }>(
      `/projects/${currentProject()}/analytics`,
    )).analytics;
  } catch (e) {
    notifyError(e);
  }
}
onMounted(load);

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
    <button class="ghost" @click="load">刷新</button>

    <template v-if="data">
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

      <div class="sect" v-if="data.underdeveloped_factions.length">
        <h3>欠发育势力（无成员）</h3>
        <span v-for="f in data.underdeveloped_factions" :key="f.id" class="chip">{{ f.name }}</span>
      </div>

      <div class="sect" v-if="data.factions.length">
        <h3>势力成员数</h3>
        <div v-for="f in data.factions" :key="f.id" class="row">
          <span>{{ f.name }}</span><span class="muted mono">{{ f.members }} 名成员</span>
        </div>
      </div>

      <div class="sect">
        <h3>待补缺口</h3>
        <div v-for="(ids, k) in data.gaps" :key="k" class="row">
          <span>{{ GAP_LABEL[k] ?? k }}</span>
          <span class="muted mono">{{ ids.length }} 项</span>
        </div>
      </div>
    </template>
    <p v-else class="muted empty">还没打开世界，或正在加载。</p>
  </section>
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
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
  color: var(--ow-ink-dim);
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
</style>
