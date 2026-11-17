<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { apiGet, apiPost, humanizeError } from "../api";
import type { NarrativeProject } from "../storyCreatorApi";

const props = defineProps<{ workspace: string; projectId: string; locked?: boolean }>();
type ToolCheck = {
  id: string; status: string; run_id?: string; job_id?: string; project_hash?: string;
  version_id?: string; activation_id?: string; error?: string | null;
  validation?: unknown; native_result?: unknown; attempts?: unknown;
};
type ToolVersion = { id: string; version?: number | string; status?: string; activation_id?: string; current_active?: boolean; passed?: boolean };
const work = ref<NarrativeProject | null>(null);
const check = ref<ToolCheck | null>(null);
const versions = ref<ToolVersion[]>([]);
const busy = ref(false);
const error = ref("");
const reason = ref("");
const rollbackId = ref("");
let epoch = 0;
let readSequence = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const base = computed(() => `/projects/${encodeURIComponent(props.workspace)}/creative/v2`);
const waiting = computed(() => ["queued", "running"].includes(check.value?.status ?? ""));
const activeActivation = computed(() => versions.value.find((entry) => entry.current_active)?.activation_id);
const historicalResult = computed(() => Boolean(check.value?.project_hash && work.value && check.value.project_hash !== work.value.project_hash));
const statusText = computed(() => ({ queued: "等待执行", running: "检查中", passed: "已完成路线检查", failed: "本次检查失败", unknown: "结果尚待核对，暂不能重新检查" }[check.value?.status ?? ""] ?? check.value?.status ?? "尚未检查"));
const result = computed<{ reachable: string[]; unreachable: string[] } | null>(() => {
  const value = check.value?.native_result;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  return Array.isArray(record.reachable) && Array.isArray(record.unreachable)
    ? { reachable: record.reachable.filter((id): id is string => typeof id === "string"), unreachable: record.unreachable.filter((id): id is string => typeof id === "string") } : null;
});
function title(id: string): string { return work.value?.outline?.scenes.find((scene) => scene.id === id)?.title ?? id; }
function storageKey(): string { return `owc:generated-story-check:${props.workspace}:${props.projectId}`; }
async function refresh(): Promise<void> {
  if (!props.projectId) return;
  if (timer) clearTimeout(timer);
  const version = epoch;
  const sequence = ++readSequence;
  const prefix = base.value;
  const id = check.value?.id;
  try {
    const [detail, library, state] = await Promise.all([
      apiGet<{ project: NarrativeProject }>(`${prefix}/narrative-projects/${encodeURIComponent(props.projectId)}`),
      apiGet<{ versions: ToolVersion[] }>(`${prefix}/generated-story-tools/versions?narrative_project_id=${encodeURIComponent(props.projectId)}`),
      id ? apiGet<ToolCheck>(`${prefix}/generated-story-tools/checks/${encodeURIComponent(id)}`) : Promise.resolve(null),
    ]);
    if (version !== epoch || sequence !== readSequence) return;
    work.value = detail.project; versions.value = library.versions;
    if (state) check.value = state;
    if (waiting.value) timer = setTimeout(() => void refresh(), 4000);
  } catch (cause) { if (version === epoch) error.value = humanizeError(cause); }
}
async function start(): Promise<void> {
  if (!work.value?.outline || props.locked || busy.value || waiting.value) return;
  busy.value = true; error.value = "";
  const version = epoch;
  try {
    const detail = await apiGet<{ project: NarrativeProject }>(`${base.value}/narrative-projects/${encodeURIComponent(props.projectId)}`);
    if (version !== epoch) return;
    work.value = detail.project;
    const reply = await apiPost<{ check_id: string; status: string; run_id?: string; job_id?: string }>(`${base.value}/generated-story-tools/check`, {
      narrative_project_id: props.projectId, expected_project_hash: work.value.project_hash,
      ...(reason.value.trim() ? { attempt_reason: reason.value.trim() } : {}),
    });
    if (version !== epoch) return;
    check.value = { id: reply.check_id, status: reply.status, run_id: reply.run_id, job_id: reply.job_id };
    sessionStorage.setItem(storageKey(), reply.check_id);
    await refresh();
  } catch (cause) { if (version === epoch) error.value = humanizeError(cause); }
  finally { if (version === epoch) busy.value = false; }
}
async function activationAction(kind: "revoke" | "rollback" | "activate"): Promise<void> {
  if (busy.value || !reason.value.trim()) return;
  if (kind !== "activate" && !activeActivation.value) return;
  if (kind === "activate" && !check.value?.version_id) return;
  const version = epoch; busy.value = true; error.value = "";
  try {
    const path = kind === "activate"
      ? `${base.value}/generated-story-tools/versions/${encodeURIComponent(check.value!.version_id!)}/activate`
      : `${base.value}/generated-story-tools/activations/${encodeURIComponent(activeActivation.value!)}/${kind}`;
    await apiPost(path, {
      reason: reason.value.trim(), actor_kind: "author", ...(kind === "rollback" ? { to_version_id: rollbackId.value } : {}),
    });
    if (version === epoch) await refresh();
  } catch (cause) { if (version === epoch) error.value = humanizeError(cause); }
  finally { if (version === epoch) busy.value = false; }
}
watch(() => [props.workspace, props.projectId], () => {
  epoch += 1; if (timer) clearTimeout(timer);
  work.value = null; check.value = null; versions.value = []; error.value = ""; busy.value = false;
  reason.value = ""; rollbackId.value = "";
  if (props.projectId) {
    const saved = sessionStorage.getItem(storageKey());
    if (saved) check.value = { id: saved, status: "loading" };
    void refresh();
  }
}, { immediate: true });
onUnmounted(() => { epoch += 1; if (timer) clearTimeout(timer); });
</script>

<template>
  <section v-if="projectId" class="tool-panel">
    <div class="tool-heading"><div><h3>检查故事路线</h3><p>查找尚未连到故事入口的场景。</p></div><button :disabled="busy || locked || waiting || !work?.outline || check?.status === 'unknown'" @click="start">{{ waiting ? '正在检查…' : '检查路线' }}</button></div>
    <p class="scope">仅检查场景连线，不包含进入条件和剧情质量。</p>
    <p v-if="error" role="alert" class="tool-error">{{ error }}</p>
    <p role="status">{{ statusText }} <button class="text-button" :disabled="busy" @click="refresh">刷新结果</button></p>
    <p v-if="check?.error" class="tool-error">{{ check.error }}</p>
    <p v-if="historicalResult" class="tool-error">此结果属于先前作品版本。请检查路线后再使用。</p>
    <div v-if="result" class="tool-result"><p><strong>可到达：</strong>{{ result.reachable.map(title).join('、') || '无' }}</p><p><strong>尚未连接：</strong>{{ result.unreachable.map(title).join('、') || '无' }}</p></div>
    <details v-if="check"><summary>检查工具与执行记录</summary><p>工具版本：{{ check.version_id ?? '尚未生成' }}。样例验证见下方记录。</p>
      <label>操作原因（必填）<input v-model="reason" /></label>
      <div class="cap-actions"><button :disabled="busy || !activeActivation || !reason.trim()" @click="activationAction('revoke')">撤销此检查能力</button><button v-if="!activeActivation" :disabled="busy || !check.version_id || !reason.trim()" @click="activationAction('activate')">重新启用已验证版本</button><label>回退版本<select v-model="rollbackId"><option value="">选择已验证旧版本</option><option v-for="item in versions.filter((entry) => entry.passed && entry.id !== check?.version_id)" :key="item.id" :value="item.id">{{ item.version ?? item.id.slice(0, 10) }}</option></select></label><button :disabled="busy || !rollbackId || !reason.trim() || !activeActivation" @click="activationAction('rollback')">回退检查能力</button></div>
      <details><summary>验证和调用记录</summary><pre>{{ JSON.stringify(check, null, 2) }}</pre></details>
    </details>
  </section>
</template>

<style scoped>
.tool-panel{border:1px solid var(--border,#d6dce3);border-radius:14px;padding:18px;margin:16px 0;background:var(--surface,#fff)}
.tool-heading{display:flex;gap:20px;justify-content:space-between;align-items:center}.tool-heading h3{margin:0 0 8px}.tool-heading p,.scope{font-size:13px;line-height:1.6;color:var(--text-muted,#5d6875)}
button,input,select{font:inherit;padding:8px 12px;border:1px solid #cbd5df;border-radius:8px}button{cursor:pointer;white-space:nowrap}button:disabled{cursor:default;opacity:.55}.text-button{padding:4px 8px;margin-left:8px}.tool-error{color:#a52727;overflow-wrap:anywhere}.tool-result{padding:8px 14px;background:#f0f7f4;border-radius:10px}summary{cursor:pointer;margin:10px 0}label{display:grid;gap:7px}input{width:100%;box-sizing:border-box}.cap-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}pre{font-size:12px;overflow:auto;max-height:320px;white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:700px){.tool-heading{display:block}.tool-heading button{margin-top:10px}}
</style>
