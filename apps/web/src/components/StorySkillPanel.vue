<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { humanizeError } from "../api";
import {
  skillCardsClient, type SkillCard, type SkillCardJob, type SkillCardStep,
  type SkillCardSelection, type SkillPackageManifest,
} from "../skillCardsApi";

const props = defineProps<{
  project: string; runId: string; disabled?: boolean;
  modelValue: SkillCardSelection[]; applyProjectSkills: boolean;
  autoSummarize: boolean; summaryJobId?: string | null;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: SkillCardSelection[]];
  "update:applyProjectSkills": [value: boolean];
  "update:autoSummarize": [value: boolean];
  working: [value: boolean];
}>();
const cards = ref<SkillCard[]>([]);
const cardId = ref("");
const versionId = ref("");
const loading = ref(false);
const working = ref(false);
const error = ref("");
const notice = ref("");
const name = ref("");
const feedback = ref("");
const job = ref<SkillCardJob | null>(null);
const permissionUncertain = ref(false);
const submissionUncertain = ref(false);
let generation = 0;
let readSequence = 0;
let jobReadSequence = 0;
let alive = true;
let timer: ReturnType<typeof setTimeout> | undefined;
let pendingOperation: { key: string; id: string } | null = null;

const card = computed(() => cards.value.find(item => item.id === cardId.value));
const version = computed(() => card.value?.versions.find(item => item.id === versionId.value));
const definition = computed(() => version.value?.definition
  ?? (version.value?.id === card.value?.current_version_id ? card.value : null));
const activeVersion = computed(() => card.value?.versions.find(item => item.id === card.value?.active_version_id));
const selected = computed(() => props.modelValue.some(item => item.card_id === card.value?.id
  && item.version_id === version.value?.id && item.version_hash === version.value?.version_hash));
const usable = computed(() => Boolean(card.value?.enabled && version.value && definition.value
  && ["candidate", "accepted"].includes(version.value.status)));
const pendingJob = computed(() => Boolean(job.value && ["queued", "running"].includes(job.value.status)));
const unavailable = computed(() => Boolean(props.disabled || working.value || loading.value || permissionUncertain.value));
const selectionLabels = computed(() => props.modelValue.map(item => {
  const found = cards.value.find(row => row.id === item.card_id);
  const chosen = found?.versions.find(row => row.id === item.version_id);
  return found && chosen ? `${chosen.definition?.name ?? found.name} v${chosen.version}` : "待读回的 Skill";
}));
const recordingIssue = computed(() => {
  let result = job.value?.result;
  for (let depth = 0; depth < 4 && result; depth += 1) {
    if (result.maintenance_recording_error?.trim()) return result.maintenance_recording_error.trim();
    result = result.status === "completed" ? result.result : null;
  }
  return "";
});

function statusLabel(status: string): string {
  return ({ candidate: "更新候选", accepted: "已确认", rejected: "未采用", retired: "已退役" } as Record<string, string>)[status] ?? "状态待核对";
}
function friendlyInstruction(step: SkillCardStep): string {
  const labels: Record<string, string> = {
    stage_count: "本次阶段数量", decision_count: "本次选择次数",
    decision_option_count: "本次选项数量", distinct_outcomes: "本次选择后果要求",
    participant_ids: "本次角色列表", game_state_variables: "本次状态变量",
    business_roles: "本次人物分工", business_relations: "本次人物关系约定",
  };
  const slots = new Map((step.slots ?? []).map(slot => [slot.name, slot]));
  return step.instruction.replace(/\{\{([A-Za-z][A-Za-z0-9_]*)\}\}/g, (_, name: string) => {
    const slot = slots.get(name);
    let label = "本次约定参数";
    if (slot?.kind === "requirement") label = labels[slot.binding_key] ?? label;
    if (slot?.kind === "entity") {
      const match = /^participant:(0|[1-9]\d*)$/.exec(slot.binding_key);
      const index = match ? Number(match[1]) : NaN;
      if (Number.isSafeInteger(index) && index < Number.MAX_SAFE_INTEGER) label = `本次第 ${index + 1} 位角色`;
    }
    return `【${label}】`;
  });
}
function jobOutcome(value: SkillCardJob) {
  if (["queued", "running"].includes(value.status)) return { kind: "pending", label: value.status === "queued" ? "排队中" : "进行中", message: "", result: null };
  if (value.status === "failed") return { kind: "failed", label: "整理失败", message: value.error || "这次整理失败，请查看任务记录。", result: null };
  let result = value.result;
  for (let depth = 0; depth < 3 && result?.status === "completed" && result.result; depth += 1) result = result.result;
  if (!["done", "succeeded"].includes(value.status)) return { kind: "unknown", label: "结果待核对", message: "整理任务状态尚未确认，请核对任务记录。", result };
  if (result?.status === "candidate" && result.card_id && result.version_id) return { kind: "candidate", label: "候选已记录", message: value.result?.new_dispatch === false ? "已读回此前整理的 Skill 候选，没有重复调用。" : "Skill 候选已生成。查看步骤和改动，再决定如何使用。", result };
  if (result?.status === "no_change") return { kind: "no_change", label: "无需更新", message: `本次无需新增或更新 Skill。${result.reason ? ` ${result.reason}` : ""}`, result };
  if (result?.status === "completed_failed") return { kind: "failed", label: "整理失败", message: result.error || result.result?.error || result.reason || "此前整理未成功，不会自动重复调用，请核对任务记录。", result };
  return { kind: "unknown", label: "结果待核对", message: result?.reason || "整理结果尚未确认，请核对执行记录；不会自动重复提交。", result };
}
function stopTimer(): void { if (timer) clearTimeout(timer); timer = undefined; }
function setWorking(value: boolean): void { working.value = value; emit("working", value); }
function rememberJob(value: SkillCardJob): void {
  try {
    const key = `owc:skill-job:${props.project}`;
    if (["queued", "running"].includes(value.status)) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch { /* Observation remains available when browser storage is unavailable. */ }
}
function recoverJob(): void {
  try {
    const stored = sessionStorage.getItem(`owc:skill-job:${props.project}`);
    if (!stored) return;
    const value = JSON.parse(stored) as Partial<SkillCardJob>;
    if (typeof value.job_id !== "string" || !value.job_id || !["queued", "running"].includes(value.status ?? "")) return;
    job.value = { job_id: value.job_id, status: value.status!, source_run_id: value.source_run_id };
    void pollJob(value.job_id, generation, props.project);
  } catch { error.value = "上次 Skill 整理任务的本地记录无法读取，请核对任务记录。"; }
}
function chooseCard(): void {
  versionId.value = card.value?.current_version_id ?? "";
  feedback.value = "";
}
function operationId(key: string): string {
  if (pendingOperation?.key !== key) pendingOperation = { key, id: crypto.randomUUID() };
  return pendingOperation.id;
}
function reconcileSelections(): void {
  const valid = props.modelValue.filter(item => {
    const found = cards.value.find(row => row.id === item.card_id);
    const chosen = found?.versions.find(row => row.id === item.version_id);
    return found?.enabled && chosen?.version_hash === item.version_hash
      && ["candidate", "accepted"].includes(chosen.status);
  });
  if (valid.length !== props.modelValue.length) emit("update:modelValue", valid);
}
async function readCards(): Promise<void> {
  const stamp = generation;
  const sequence = ++readSequence;
  const project = props.project;
  if (!project) { cards.value = []; loading.value = false; return; }
  loading.value = true; error.value = "";
  try {
    const result = await skillCardsClient(project).list();
    if (!alive || stamp !== generation || sequence !== readSequence || project !== props.project) return;
    cards.value = result.cards;
    permissionUncertain.value = false;
    if (!cards.value.some(item => item.id === cardId.value)) cardId.value = cards.value[0]?.id ?? "";
    if (!card.value?.versions.some(item => item.id === versionId.value)) chooseCard();
    reconcileSelections();
  } catch (cause) {
    if (alive && stamp === generation && sequence === readSequence) {
      permissionUncertain.value = true;
      emit("update:modelValue", []);
      error.value = humanizeError(cause);
    }
  } finally {
    if (alive && stamp === generation && sequence === readSequence) loading.value = false;
  }
}
function useOnce(): void {
  const current = card.value; const chosen = version.value;
  if (unavailable.value || !current || !chosen || !usable.value) return;
  const wasSelected = selected.value;
  const other = props.modelValue.filter(item => item.card_id !== current.id);
  emit("update:modelValue", wasSelected ? other : [...other, {
    card_id: current.id, version_id: chosen.id, version_hash: chosen.version_hash,
  }]);
  notice.value = wasSelected ? "已取消本次选用。" : "下次生成使用此版；本次选用不会改变后续设置。";
}
async function mutate(action: (client: ReturnType<typeof skillCardsClient>) => Promise<SkillCard>, message: string): Promise<void> {
  if (unavailable.value) return;
  const stamp = generation; const project = props.project;
  setWorking(true); error.value = ""; notice.value = "";
  try {
    const result = await action(skillCardsClient(project));
    if (!alive || stamp !== generation || project !== props.project) return;
    cards.value = [result, ...cards.value.filter(item => item.id !== result.id)];
    cardId.value = result.id;
    if (!result.versions.some(item => item.id === versionId.value)) chooseCard();
    reconcileSelections(); notice.value = message;
  } catch (cause) {
    if (alive && stamp === generation) {
      permissionUncertain.value = true; emit("update:modelValue", []);
      error.value = `${humanizeError(cause)} 请刷新 Skill 状态后继续。`;
    }
  } finally {
    if (alive && stamp === generation) setWorking(false);
  }
}
async function adopt(): Promise<void> {
  const current = card.value; const chosen = version.value;
  if (!current || !chosen || !usable.value) return;
  await mutate(client => client.adopt(current, chosen.id, true), "已采用此版，后续任务可使用。当前故事内容保持原样。");
}
async function setEnabled(): Promise<void> {
  const current = card.value;
  if (!current) return;
  await mutate(client => client.enable(current, !current.enabled), current.enabled ? "已停用此 Skill。" : "已启用此 Skill。");
}
async function rollback(): Promise<void> {
  const current = card.value; const chosen = version.value;
  if (!current || !chosen || chosen.status !== "accepted") return;
  await mutate(client => client.rollback(current, chosen.id), "后续任务已退回所选版本；历史运行保留。");
}
async function pollJob(jobId: string, stamp: number, project: string): Promise<void> {
  stopTimer();
  const sequence = ++jobReadSequence;
  try {
    const result = await skillCardsClient(project).job(jobId);
    if (!alive || stamp !== generation || sequence !== jobReadSequence || project !== props.project || job.value?.job_id !== jobId) return;
    job.value = { ...result, source_run_id: job.value?.source_run_id };
    rememberJob(job.value);
    if (["queued", "running"].includes(result.status)) {
      timer = setTimeout(() => { void pollJob(jobId, stamp, project); }, 2500);
    } else {
      pendingOperation = null;
      await readCards();
      if (alive && stamp === generation && sequence === jobReadSequence) {
        const outcome = jobOutcome(result);
        notice.value = "";
        if (!permissionUncertain.value) error.value = "";
        if (outcome.kind === "candidate") {
          const found = cards.value.find(item => item.id === outcome.result?.card_id);
          const recorded = found?.versions.find(item => item.id === outcome.result?.version_id);
          if (!permissionUncertain.value && found && recorded) {
            cardId.value = found.id; versionId.value = recorded.id; feedback.value = "";
            notice.value = outcome.message;
          } else error.value = "候选记录尚未读回，请刷新 Skill 状态核对；不会自动重新整理。";
        } else if (outcome.kind === "no_change") notice.value = outcome.message;
        else error.value = outcome.message;
      }
    }
  } catch (cause) {
    if (alive && stamp === generation && sequence === jobReadSequence) {
      error.value = `${humanizeError(cause)} 整理任务可能仍在运行，请读取任务状态，不要重复提交。`;
    }
  }
}
async function summarize(update: boolean): Promise<void> {
  if (unavailable.value || pendingJob.value || !props.runId || (update && (!card.value || !feedback.value.trim()))) return;
  const stamp = generation; const project = props.project;
  const runId = props.runId; const current = card.value; const baseVersion = version.value;
  const text = feedback.value.trim(); const displayName = name.value.trim();
  const key = JSON.stringify([project, update ? current?.id : null, update ? current?.card_hash : null, update ? baseVersion?.id : null, runId, update ? text : displayName]);
  if (submissionUncertain.value && pendingOperation?.key !== key) {
    error.value = "上次提交结果尚未读回，请保持原输入重试以核对同一提交。";
    return;
  }
  const id = operationId(key);
  setWorking(true); error.value = ""; notice.value = "";
  try {
    const client = skillCardsClient(project);
    const result = update ? await client.update(current!, baseVersion!.id, runId, text, id) : await client.fromRun(runId, id, displayName || undefined);
    if (!alive || stamp !== generation || project !== props.project) return;
    submissionUncertain.value = false;
    job.value = { ...result, source_run_id: runId };
    rememberJob(job.value);
    setWorking(false);
    await pollJob(result.job_id, stamp, project);
  } catch (cause) {
    if (alive && stamp === generation) {
      setWorking(false);
      submissionUncertain.value = true;
      error.value = `${humanizeError(cause)} 重试相同请求会沿用本次提交编号。`;
    }
  }
}
function readManifest(text: string): SkillPackageManifest {
  const value = JSON.parse(text) as unknown;
  if (!value || typeof value !== "object" || !Array.isArray((value as SkillPackageManifest).files)) {
    throw new Error("请选择导出的 Skill 文件清单 JSON。");
  }
  const files = (value as SkillPackageManifest).files;
  if (!files.length || files.length > 128 || files.some(file => !file || typeof file.path !== "string" || typeof file.content !== "string")) {
    throw new Error("文件清单缺少路径或文字内容。");
  }
  return { files: files.map(file => ({ path: file.path, content: file.content })) };
}
async function importFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file || unavailable.value) return;
  if (file.size > 2 * 1024 * 1024) { error.value = "文件清单超过 2 MB，请缩小后导入。"; return; }
  const stamp = generation; const project = props.project;
  setWorking(true); error.value = ""; notice.value = "";
  try {
    const manifest = readManifest(await file.text());
    if (!alive || stamp !== generation || project !== props.project) return;
    const result = await skillCardsClient(project).importPackage(manifest);
    if (!alive || stamp !== generation || project !== props.project) return;
    cards.value = [result, ...cards.value.filter(item => item.id !== result.id)];
    cardId.value = result.id; chooseCard();
    notice.value = "已导入为候选；尚未用于故事或后续任务。";
  } catch (cause) {
    if (alive && stamp === generation) error.value = humanizeError(cause);
  } finally {
    if (alive && stamp === generation) setWorking(false);
  }
}
async function exportFile(manifest: boolean): Promise<void> {
  const current = card.value; const chosen = version.value;
  if (unavailable.value || !current || !chosen) return;
  const stamp = generation; const project = props.project;
  setWorking(true); error.value = "";
  try {
    const client = skillCardsClient(project);
    if (manifest) {
      const result = await client.exportManifest(current.id, chosen.id);
      if (!alive || stamp !== generation || project !== props.project) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url; link.download = `skill-v${chosen.version}.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } else await client.exportPackage(current.id, chosen.id);
  } catch (cause) {
    if (alive && stamp === generation) error.value = humanizeError(cause);
  } finally {
    if (alive && stamp === generation) setWorking(false);
  }
}
watch(() => props.project, () => {
  generation += 1; readSequence += 1; jobReadSequence += 1; stopTimer(); pendingOperation = null;
  cards.value = []; cardId.value = ""; versionId.value = ""; job.value = null;
  name.value = ""; feedback.value = ""; error.value = ""; notice.value = "";
  permissionUncertain.value = false; submissionUncertain.value = false; loading.value = false; setWorking(false);
  emit("update:modelValue", []); void readCards(); recoverJob();
}, { immediate: true });
watch(() => [props.runId, props.summaryJobId] as const, ([runId, jobId], previous) => {
  if (previous && runId !== previous[0]) void readCards();
  if (!jobId || job.value?.job_id === jobId) return;
  stopTimer();
  job.value = { job_id: jobId, status: "queued", source_run_id: runId };
  rememberJob(job.value);
  void pollJob(jobId, generation, props.project);
}, { immediate: true });
onBeforeUnmount(() => { alive = false; generation += 1; stopTimer(); emit("working", false); });
</script>

<template>
  <section class="skill-panel" aria-label="制作 Skill">
    <header><div><h2>制作 Skill</h2><p class="hint">把有效的制作方法留下，之后按需使用。</p></div><button :disabled="working || loading" @click="readCards">{{ loading ? '读取中…' : '刷新' }}</button></header>
    <label class="check"><input type="checkbox" :checked="applyProjectSkills" :disabled="disabled" @change="emit('update:applyProjectSkills', ($event.target as HTMLInputElement).checked)">使用已采用的 Skill</label>
    <label class="check"><input type="checkbox" :checked="autoSummarize" :disabled="disabled" @change="emit('update:autoSummarize', ($event.target as HTMLInputElement).checked)">制作结束后整理 Skill 候选</label>
    <p v-if="selectionLabels.length" class="selection-summary">仅下次生成：{{ selectionLabels.join('、') }} <button class="text-link" :disabled="disabled || working" @click="emit('update:modelValue', [])">清空</button></p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="recordingIssue" class="recording-warning" role="status">维护记录尚未关联完成，请核对记录。{{ recordingIssue }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="cards.length" class="pickers"><label>Skill<select v-model="cardId" :disabled="working || loading" @change="chooseCard"><option v-for="item in cards" :key="item.id" :value="item.id">{{ item.name }}{{ item.enabled ? '' : ' · 已停用' }}</option></select></label><label v-if="card">版本<select v-model="versionId" :disabled="working || loading"><option v-for="item in [...card.versions].reverse()" :key="item.id" :value="item.id">v{{ item.version }} · {{ statusLabel(item.status) }}{{ card.active_version_id === item.id ? ' · 后续使用' : '' }}</option></select></label></div>
    <article v-if="card && version" class="skill-card">
      <div class="card-title"><h3>{{ definition?.name ?? card.name }}</h3><span>v{{ version.version }} · {{ statusLabel(version.status) }}</span></div>
      <template v-if="definition"><p class="purpose">{{ definition.purpose }}</p><ol class="steps"><li v-for="(step, index) in definition.steps" :key="index"><strong>{{ step.title }}</strong><p>{{ friendlyInstruction(step) }}</p><details v-if="step.slots?.length"><summary>模板与参数</summary><pre>{{ JSON.stringify({ instruction: step.instruction, slots: step.slots }, null, 2) }}</pre></details></li></ol><details v-if="definition.applies_to.length || definition.does_not_apply_to.length"><summary>适用范围</summary><div class="scope-grid"><section v-if="definition.applies_to.length"><h4>适合</h4><ul><li v-for="item in definition.applies_to" :key="item">{{ item }}</li></ul></section><section v-if="definition.does_not_apply_to.length"><h4>不适合</h4><ul><li v-for="item in definition.does_not_apply_to" :key="item">{{ item }}</li></ul></section></div></details></template>
      <p v-else class="hint">此历史版本没有可读的步骤记录，暂不支持本次选用。</p>
      <section v-if="version.changes.length" class="changes"><h4>这版改动</h4><ul><li v-for="item in version.changes" :key="item">{{ item }}</li></ul></section>
      <p class="source">来源：{{ card.source_summary || '尚无来源说明' }}</p>
      <p v-if="card.maintenance_notice" class="error">{{ card.maintenance_notice }}</p>
      <p v-if="!card.enabled" class="hint">此 Skill 已停用。</p><p v-else-if="activeVersion" class="hint">后续使用 v{{ activeVersion.version }}；本次选择不会替换它。</p>
      <div class="actions"><button :class="{ selected }" :disabled="unavailable || !usable" :aria-pressed="selected" @click="useOnce">{{ selected ? '取消本次使用' : '仅本次使用' }}</button><button class="primary" :disabled="unavailable || !usable || card.active_version_id === version.id" @click="adopt">后续任务使用</button><button v-if="version.status === 'accepted' && card.active_version_id !== version.id" :disabled="unavailable" @click="rollback">退回此版</button><button :disabled="unavailable" @click="setEnabled">{{ card.enabled ? '停用' : '启用' }}</button></div>
      <details class="update"><summary>更新这个 Skill</summary><label>想怎样调整<textarea v-model="feedback" rows="3" maxlength="8000" :disabled="disabled || working" /></label><button :disabled="unavailable || pendingJob || !runId || !feedback.trim()" @click="summarize(true)">生成更新候选</button><p v-if="!runId" class="hint">打开一次运行后，可结合运行记录更新。</p></details>
      <details><summary>导出</summary><div class="actions"><button :disabled="unavailable" @click="exportFile(false)">标准 Skill 包（ZIP）</button><button :disabled="unavailable" @click="exportFile(true)">文件清单（JSON）</button></div></details>
    </article>
    <p v-else-if="!loading" class="empty">尚无 Skill。完成一次制作后，可以把方法整理成候选。</p>
    <details class="create"><summary>从当前运行总结为 Skill</summary><label>名称（选填）<input v-model="name" maxlength="200" :disabled="disabled || working"></label><button :disabled="unavailable || pendingJob || !runId" @click="summarize(false)">总结为 Skill</button><p v-if="!runId" class="hint">先打开或完成一次运行。</p></details>
    <p v-if="job" class="hint">整理任务：{{ jobOutcome(job).label }} <button v-if="!working && ['pending', 'unknown'].includes(jobOutcome(job).kind)" :disabled="disabled" @click="pollJob(job.job_id, generation, project)">读取任务状态</button></p>
    <details class="import"><summary>导入 Skill</summary><label>文件清单（JSON）<input type="file" accept=".json,application/json" :disabled="unavailable" @change="importFile"></label></details>
  </section>
</template>

<style scoped>
.skill-panel { display: grid; gap: .8rem; }
header, .card-title, .actions { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; }
header, .card-title { justify-content: space-between; }
h2, h3, h4, p { margin: 0; } h2 { font: 1.05rem var(--ow-display); } h3 { font-size: .98rem; } h4 { font-size: .79rem; font-weight: 600; }
.hint, .source, .card-title > span { color: var(--ow-ink-mute); font-size: .75rem; line-height: 1.8; }
.skill-card { display: grid; gap: .85rem; border: 1px solid var(--ow-paper-edge); border-radius: 8px; padding: 1rem; }
.purpose { color: var(--ow-ink-soft); font-size: .88rem; line-height: 1.8; }
.steps { margin: 0; padding-left: 1.4rem; } .steps li { padding: .4rem 0 .4rem .2rem; } .steps strong { font-size: .82rem; } .steps pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 16rem; overflow: auto; font-size: .7rem; line-height: 1.7; } .steps p { margin-top: .25rem; color: var(--ow-ink-soft); font-size: .79rem; line-height: 1.8; white-space: pre-wrap; }
label { display: grid; gap: .4rem; color: var(--ow-ink-soft); font-size: .79rem; }
.check { display: flex; gap: .55rem; align-items: center; }.check input { width: auto; }
.pickers, .scope-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
input, select, textarea { width: 100%; min-width: 0; padding: .55rem .65rem; border: 1px solid var(--ow-paper-edge); border-radius: 5px; background: rgba(12,12,25,.3); color: inherit; font: inherit; }
textarea { resize: vertical; line-height: 1.8; }
button { border: 1px solid var(--ow-paper-edge); border-radius: 5px; padding: .45rem .65rem; color: inherit; background: transparent; font: inherit; font-size: .77rem; cursor: pointer; }
button:disabled { opacity: .45; cursor: default; } button.primary, button.selected { color: var(--ow-gold); border-color: var(--ow-gold-soft); background: var(--ow-gold-faint); }
.text-link { padding: .1rem; border: 0; color: var(--creator-teal, var(--ow-gold)); }
input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible { outline: 2px solid var(--ow-gold-soft); outline-offset: 2px; }
details { border-top: 1px solid var(--ow-paper-edge); padding-top: .65rem; } summary { cursor: pointer; font-size: .78rem; color: var(--ow-ink-soft); } details > label, details > button, details > .actions, .scope-grid { margin-top: .7rem; }
ul { padding-left: 1.2rem; margin: .4rem 0 0; font-size: .78rem; color: var(--ow-ink-soft); line-height: 1.8; }
.selection-summary, .notice, .error, .empty { font-size: .8rem; line-height: 1.8; }.selection-summary, .notice { color: var(--ow-gold); }.error { color: #e7a89a; }.empty { color: var(--ow-ink-mute); }
.recording-warning { color: var(--ow-gold); font-size: .77rem; line-height: 1.8; }
@media (max-width: 600px) { .pickers, .scope-grid { grid-template-columns: 1fr; } .skill-card { padding: .8rem; } }
</style>
