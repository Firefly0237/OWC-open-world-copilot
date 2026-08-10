<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, apiPost, currentProject } from "../api";
import { notifyError, notifyOk } from "../toast";
import PageHead from "../components/PageHead.vue";

interface Param {
  key: string;
  label: string;
  required: boolean;
}
interface Template {
  id: string;
  name: string;
  kind: string;
  description: string;
  params: Param[];
}

const templates = ref<Template[]>([]);
const picked = ref<Template | null>(null);
const values = ref<Record<string, string>>({});
const busy = ref(false);
const loading = ref(false);
const loadError = ref("");
const missingRequired = computed(() =>
  (picked.value?.params ?? []).filter(
    (param) => param.required && !(values.value[param.key] ?? "").trim(),
  ),
);

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    templates.value = (await apiGet<{ templates: Template[] }>("/templates")).templates;
  } catch (e) {
    loadError.value = "模板库暂时无法加载。";
    notifyError(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);

function pick(t: Template): void {
  picked.value = t;
  values.value = Object.fromEntries(t.params.map((p) => [p.key, ""]));
}

async function instantiate(): Promise<void> {
  if (!picked.value || missingRequired.value.length > 0) {
    if (missingRequired.value.length > 0) notifyError("请先填写所有必填参数。");
    return;
  }
  busy.value = true;
  try {
    const res = await apiPost<{ created: { quests: string[]; entities: string[] } }>(
      `/projects/${currentProject()}/templates:instantiate`,
      { template_id: picked.value.id, params: values.value },
    );
    const n = res.created.quests.length + res.created.entities.length;
    notifyOk(`已按模板生成 ${n} 项，去审阅台采纳。`);
    picked.value = null;
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead
      overline="TEMPLATES"
      title="模板库"
      purpose="从原型一键起草：填几个参数即生成任务/势力，仍进审阅台由你定夺。"
    />
    <p v-if="loading" class="pane state" role="status">正在加载模板…</p>
    <div v-else-if="loadError" class="pane state error-state" role="alert">
      <span>{{ loadError }}</span>
      <button type="button" class="ghost" @click="load">重试</button>
    </div>
    <p v-else-if="!templates.length" class="pane state">当前没有可用模板。</p>
    <div v-else class="grid">
      <button
        v-for="t in templates"
        :key="t.id"
        type="button"
        class="tpl"
        :class="{ on: picked?.id === t.id }"
        @click="pick(t)"
      >
        <span class="tpl-kind">{{ t.kind === "quest" ? "任务" : "势力" }}</span>
        <span class="tpl-name">{{ t.name }}</span>
        <span class="tpl-desc">{{ t.description }}</span>
      </button>
    </div>

    <form v-if="picked" class="form pane" @submit.prevent="instantiate">
      <h3>{{ picked.name }}</h3>
      <label v-for="p in picked.params" :key="p.key" class="f">
        <span>{{ p.label }}<em v-if="p.required" class="req">*</em></span>
        <input
          v-model="values[p.key]"
          :placeholder="p.key"
          :required="p.required"
          :aria-required="p.required"
          :aria-invalid="p.required && !(values[p.key] ?? '').trim()"
        />
      </label>
      <p v-if="missingRequired.length" class="validation" role="status">
        请填写：{{ missingRequired.map((param) => param.label).join("、") }}
      </p>
      <div class="actions">
        <button type="submit" class="primary" :disabled="busy || missingRequired.length > 0">
          {{ busy ? "生成中…" : "生成并送审" }}
        </button>
        <button type="button" class="ghost" @click="picked = null">取消</button>
      </div>
    </form>
  </section>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 1rem 0;
}
.state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.8rem;
  margin: 1rem 0;
  padding: 0.9rem 1rem;
  color: var(--ow-muted);
}
.error-state { border-color: var(--ow-flag, #e0653a); }
.tpl {
  text-align: left;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  padding: 0.8rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  color: var(--ow-ink);
}
.tpl.on {
  border-color: var(--ow-gold, #d8b46a);
}
.tpl-kind {
  font-size: 0.7rem;
  color: var(--ow-ink-dim);
}
.tpl-name {
  font-weight: 600;
}
.tpl-desc {
  font-size: 0.78rem;
  color: var(--ow-ink-dim);
}
.form {
  padding: 1rem 1.2rem;
  max-width: 30rem;
}
.f {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-bottom: 0.6rem;
  font-size: 0.82rem;
}
.f .req {
  color: var(--ow-flag, #e0653a);
  font-style: normal;
}
.f input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  color: var(--ow-ink);
  padding: 0.4rem 0.6rem;
}
.f input[aria-invalid="true"] { border-color: var(--ow-flag, #e0653a); }
.validation { color: var(--ow-flag, #e0653a); font-size: 0.8rem; }
.actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.4rem;
}
.actions .primary {
  background: var(--ow-gold, #d8b46a);
  color: #1a1406;
  border: none;
  border-radius: var(--ow-control-radius);
  padding: 0.4rem 1rem;
  cursor: pointer;
}
</style>
