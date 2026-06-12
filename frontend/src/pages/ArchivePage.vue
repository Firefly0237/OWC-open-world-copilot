<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, currentProject } from "../api";

interface EntityRow {
  id: string;
  name: string;
  type: string;
  description: string;
  origin: string;
  review_status: string;
}

const entities = ref<EntityRow[]>([]);
const query = ref("");
const error = ref("");

const TYPE_LABELS: Record<string, string> = {
  npc: "角色",
  location: "地点",
  faction: "势力",
  item: "物品",
  region: "区域",
  term: "术语",
  skill: "技能",
  achievement: "成就",
};

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  if (!needle) return entities.value;
  return entities.value.filter((row) =>
    [row.id, row.name, row.description].some((field) =>
      String(field ?? "").toLowerCase().includes(needle),
    ),
  );
});

onMounted(async () => {
  try {
    const body = await apiGet<{ inventory: { entities: EntityRow[] } }>(
      `/projects/${currentProject()}/archive`,
    );
    entities.value = body.inventory.entities;
  } catch (e) {
    error.value = String(e);
  }
});
</script>

<template>
  <section>
    <div class="section"><span class="t">设定档案 · 实体</span></div>
    <p v-if="error" class="muted">读取失败：{{ error }}</p>
    <template v-else>
      <input v-model="query" class="search" placeholder="按名称 / ID / 描述过滤…" />
      <p class="muted count">{{ filtered.length }} / {{ entities.length }} 条</p>
      <div class="pane tablewrap">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>ID</th>
              <th>描述</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in filtered" :key="row.id">
              <td class="name">{{ row.name }}</td>
              <td>{{ TYPE_LABELS[row.type] ?? row.type }}</td>
              <td class="mono">{{ row.id }}</td>
              <td class="muted">{{ row.description }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.search {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
}

.count {
  font-size: 0.78rem;
  margin: 0.4rem 0;
}

.tablewrap {
  overflow-x: auto;
  padding: 0.2rem 0.6rem;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.86rem;
}

th {
  text-align: left;
  color: var(--ow-muted);
  font-weight: 500;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--ow-line);
}

td {
  padding: 0.45rem 0.6rem;
  border-bottom: 1px solid rgba(46, 54, 88, 0.55);
  vertical-align: top;
}

.name {
  color: var(--ow-ink);
  font-weight: 600;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.78rem;
}
</style>
