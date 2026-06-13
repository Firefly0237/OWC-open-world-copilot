<script setup lang="ts">
import { computed, ref } from "vue";
import { apiPost, currentProject } from "../api";

interface Change {
  change_type: string;
  object_type: string;
  object_id: string;
}
interface Issue {
  severity: string;
  rule_code: string;
  message: string;
  target_ref: string;
}
interface IngestResult {
  dry_run: boolean;
  incoming_count: number;
  changes: Change[];
  issues: Issue[];
  has_errors: boolean;
}

const CT_LABEL: Record<string, string> = {
  add: "新增",
  update: "更新",
  unchanged: "无变化",
  conflict: "冲突",
};

const file = ref<File | null>(null);
const fileB64 = ref("");
const busy = ref(false);
const error = ref("");
const flash = ref("");
const preview = ref<IngestResult | null>(null);
const committed = ref(false);

const counts = computed(() => {
  const c: Record<string, number> = {};
  for (const ch of preview.value?.changes ?? []) c[ch.change_type] = (c[ch.change_type] ?? 0) + 1;
  return c;
});

function onFile(event: Event): void {
  const f = (event.target as HTMLInputElement).files?.[0] ?? null;
  file.value = f;
  preview.value = null;
  committed.value = false;
  flash.value = "";
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    const result = reader.result as string;
    fileB64.value = result.includes(",") ? result.split(",", 2)[1] : result;
  };
  reader.readAsDataURL(f);
}

async function call(dryRun: boolean): Promise<IngestResult | null> {
  if (!file.value || !fileB64.value) return null;
  busy.value = true;
  error.value = "";
  try {
    return await apiPost<IngestResult>(`/projects/${currentProject()}/ingest`, {
      filename: file.value.name,
      content_base64: fileB64.value,
      dry_run: dryRun,
      write_non_conflicting: !dryRun,
    });
  } catch (e) {
    error.value = String(e);
    return null;
  } finally {
    busy.value = false;
  }
}

async function dryRun(): Promise<void> {
  committed.value = false;
  flash.value = "";
  const r = await call(true);
  if (r) preview.value = r;
}

async function commit(): Promise<void> {
  const r = await call(false);
  if (r) {
    preview.value = r;
    committed.value = true;
    const writable = (r.changes ?? []).filter((c) => c.change_type === "add" || c.change_type === "update").length;
    flash.value = `已写入 ${writable} 项（冲突项已跳过，需人工处理）。`;
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">表格导入 · 严格格式入库</span></div>
    <p class="muted hint">从既有的表格/数据文件（xlsx / json / jsonl / md / luban）批量导入。先预演看清增改冲突，确认后只写非冲突项。</p>

    <div class="pane form">
      <input type="file" accept=".xlsx,.json,.jsonl,.md,.luban" @change="onFile" />
      <div class="actions">
        <button class="ghost" :disabled="busy || !file" @click="dryRun">
          {{ busy && !committed ? "预演中…" : "预演（不写入）" }}
        </button>
        <button
          class="primary"
          :disabled="busy || !preview || preview.has_errors"
          :title="preview?.has_errors ? '存在错误，先在校勘修复处理' : ''"
          @click="commit"
        >
          确认导入
        </button>
      </div>
      <p v-if="preview?.has_errors" class="muted small">预演发现错误，确认导入已锁定——请先处理下方错误。</p>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="flash" class="flash">{{ flash }}</p>

    <div v-if="preview" class="pane done">
      <div class="chips">
        <span class="chip">读入 <b>{{ preview.incoming_count }}</b></span>
        <span class="chip green">{{ CT_LABEL.add }} <b>{{ counts.add ?? 0 }}</b></span>
        <span class="chip gold">{{ CT_LABEL.update }} <b>{{ counts.update ?? 0 }}</b></span>
        <span class="chip">{{ CT_LABEL.unchanged }} <b>{{ counts.unchanged ?? 0 }}</b></span>
        <span class="chip red">{{ CT_LABEL.conflict }} <b>{{ counts.conflict ?? 0 }}</b></span>
        <span class="chip" :class="committed ? 'green' : ''">{{ committed ? "已写入" : "仅预演" }}</span>
      </div>

      <template v-if="preview.issues.length">
        <div class="section sub"><span class="t red-t">问题</span></div>
        <div class="issues">
          <div v-for="(it, i) in preview.issues" :key="i" class="issue" :class="it.severity">
            <span class="sev">{{ it.severity }}</span>
            <span class="mono">{{ it.rule_code }}</span>
            <span class="muted">{{ it.message }}</span>
          </div>
        </div>
      </template>

      <div class="section sub"><span class="t">逐项变更</span></div>
      <TransitionGroup name="list" tag="div" class="changes">
        <div v-for="(ch, i) in preview.changes" :key="i" class="change" :class="ch.change_type">
          <span class="ct">{{ CT_LABEL[ch.change_type] ?? ch.change_type }}</span>
          <span class="otype muted">{{ ch.object_type }}</span>
          <span class="mono">{{ ch.object_id }}</span>
        </div>
      </TransitionGroup>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}

.actions {
  display: flex;
  gap: 0.6rem;
}

button {
  border-radius: 0.5rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.86rem;
  padding: 0.5rem 1rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}

button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error {
  color: #e89a9a;
}

.flash {
  color: #8ed4ac;
}

.small {
  font-size: 0.78rem;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.4rem;
}

.chip {
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  background: rgba(16, 22, 48, 0.6);
  color: var(--ow-muted);
  font-size: 0.78rem;
  padding: 0.16rem 0.65rem;
}

.chip b {
  color: var(--ow-ink);
}

.chip.green {
  border-color: rgba(142, 212, 172, 0.45);
  color: #8ed4ac;
}

.chip.gold {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

.chip.red {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

.sub {
  margin-top: 0.5rem;
}

.red-t {
  color: #e89a9a !important;
}

.issues {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.3rem;
}

.issue {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  font-size: 0.82rem;
}

.sev {
  font-size: 0.72rem;
  border-radius: 999px;
  padding: 0.06rem 0.5rem;
  border: 1px solid rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

.changes {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.change {
  display: grid;
  grid-template-columns: 4rem 6rem 1fr;
  gap: 0.6rem;
  align-items: baseline;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  background: var(--ow-panel-2);
  padding: 0.4rem 0.7rem;
  font-size: 0.83rem;
}

.change.conflict {
  border-color: rgba(224, 133, 133, 0.35);
}

.ct {
  font-size: 0.76rem;
}

.change.add .ct {
  color: #8ed4ac;
}

.change.update .ct {
  color: var(--ow-gold-bright);
}

.change.conflict .ct {
  color: #e89a9a;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--ow-cyan);
}

.list-enter-active,
.list-move {
  transition: all 0.25s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(5px);
}
</style>
