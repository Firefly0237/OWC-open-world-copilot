<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { humanizeError } from "../api";
import { CREATOR_OPERATIONS, SYSTEM_CREATOR_CHECKS, storyCreatorClient, type StoryRunState } from "../storyCreatorApi";
import type { EvolutionEvent } from "../evolutionStudioApi";
import type { RunSkillUsage, SkillUsageStatus } from "../skillCardsApi";

const props = defineProps<{
  project: string; taskId: string; taskOrigin?: string; draftRevision?: number;
  run: StoryRunState | null; events: EvolutionEvent[];
  planning: boolean; gateSubject?: string;
  impact?: Record<string, unknown> | null;
}>();
const usage = ref<RunSkillUsage | null>(null);
const loading = ref(false);
const error = ref("");
let alive = true;
let generation = 0;
let sequence = 0;
let lastContext = "";

function operationName(value: string): string { return CREATOR_OPERATIONS[value] ?? "制作步骤"; }
function turnState(run: StoryRunState | null, events: EvolutionEvent[], planning: boolean, gateSubject?: string) {
  if (planning) return { actor: "AI 正在安排步骤", detail: "制作计划就绪后开始生成。", state: "working", target: "" };
  if (!run) return { actor: "等待开始", detail: "准备好要求后即可生成。", state: "idle", target: "" };
  const pending = run.collaboration?.pending_gate;
  const gate = pending ? "gate" in pending ? pending.gate : pending : null;
  const status = run.run.status;
  if (status === "completed") return { actor: "可以阅读和试玩", detail: "本轮制作已结束。", state: "ready", target: "#creator-manuscript" };
  if (status === "completed_with_skips") return { actor: "可查看已完成内容", detail: "本轮仍有未完成步骤。", state: "attention", target: "#creator-manuscript" };
  if (["cancelled", "interrupted"].includes(status)) return { actor: status === "cancelled" ? "本轮已停止" : "本轮已中断", detail: "已有内容保留。", state: "idle", target: "" };
  if (["failed", "failed_setup", "budget_exhausted", "blocked", "blocked_dependency"].includes(status)) return { actor: "制作遇到问题", detail: "查看下方原因后再决定如何继续。", state: "attention", target: "#creator-production" };
  if (run.run.cancel_requested) return { actor: "正在停止本轮", detail: "等待当前调用的状态确认。", state: "working", target: "" };
  if (gate) return {
    actor: gate.permission_required ? "等你调整约定" : "轮到你确认",
    detail: gate.permission_required ? "当前步骤尚未获准执行。" : `确认${gateSubject || gate.title || "当前产物"}后继续。`,
    state: "waiting", target: "#creator-author-turn",
  };
  if (status === "waiting_user" || status === "paused") return { actor: "停点信息待读回", detail: "刷新后查看当前需要确认的内容。", state: "attention", target: "" };
  if (status === "queued") return { actor: "等待制作开始", detail: "任务已进入队列。", state: "working", target: "" };
  if (status !== "running") return { actor: "状态待读回", detail: "刷新当前运行可核对进度。", state: "idle", target: "" };
  const active = new Set<string>();
  for (const event of [...events].sort((left, right) => left.seq - right.seq)) {
    if (!event.node_id) continue;
    if (event.type === "graph.node.started") active.add(event.node_id);
    else if (["graph.node.completed", "graph.node.reused", "graph.node.failed", "graph.node.blocked", "graph.node.cancelled"].includes(event.type)) active.delete(event.node_id);
  }
  const operations = (run.graph?.nodes ?? []).filter(node => active.has(node.id)).map(node => node.operation);
  const checking = operations.length > 0 && operations.every(operation => SYSTEM_CREATOR_CHECKS.has(operation) || ["audit", "bind_dialogue", "package"].includes(operation));
  return {
    actor: checking ? "系统正在检查与整理" : "AI 正在制作",
    detail: run.collaboration?.pause_requested ? "已请求在当前步骤结束后暂停。"
      : operations.length ? [...new Set(operations.map(operationName))].join("、") : "按本轮约定继续。",
    state: "working", target: "",
  };
}
function modeText(run: StoryRunState | null): string {
  const policy = run?.collaboration?.policy;
  if (!policy) return "本轮约定待读回";
  const planReview = typeof policy.plan_review === "boolean" ? policy.plan_review : ["direction", "edit"].includes(policy.mode);
  const stepReview = typeof policy.key_step_review === "boolean" ? policy.key_step_review : policy.mode === "edit";
  if (stepReview) return "分步确认";
  if (planReview) return "先确认大纲";
  if (policy.review_operations?.length) return "指定步骤确认";
  return "连续制作";
}
function usageStatus(value: SkillUsageStatus): string {
  return ({ bound: "已配置", dispatched: "已带入调用", response_received: "收到回应", unknown: "记录待核对" })[value] ?? "记录待核对";
}
function usageSummary(skill: RunSkillUsage["skills"][number]): string {
  if (skill.version == null || !skill.steps.length) return "版本记录待核对";
  const supplied = skill.steps.filter(step => ["dispatched", "response_received"].includes(step.status)).length;
  const responded = skill.steps.filter(step => step.status === "response_received").length;
  if (responded === skill.steps.length && responded) return `${responded} 个步骤收到回应`;
  if (supplied) return `${supplied} 个步骤带入调用${responded ? `，其中 ${responded} 个收到完整回应` : ""}`;
  if (skill.steps.some(step => step.status === "unknown")) return "部分调用记录待核对";
  return "已选用，尚无调用记录";
}
function nodeName(id: string): string {
  const operation = props.run?.graph?.nodes.find(node => node.id === id)?.operation;
  return operation ? operationName(operation) : "制作步骤";
}
const currentTurn = computed(() => turnState(props.run, props.events, props.planning, props.gateSubject));
const contextKey = computed(() => JSON.stringify([props.project, props.taskId, props.run?.run.id, props.run?.run.revision]));
const refreshKey = computed(() => JSON.stringify([contextKey.value, props.run?.run.status, props.events.at(-1)?.seq]));
const development = computed(() => props.taskOrigin === "development" || usage.value?.source?.task_origin === "development");
const summaryIssue = computed(() => props.run?.skill_summary_error?.trim() || "");
const stageOperations = computed(() => (props.run?.collaboration?.stage_agreements ?? []).flatMap(stage => stage.operations));
const aiScope = computed(() => [...new Set(stageOperations.value.filter(item => item.allowed && !item.system_required && !SYSTEM_CREATOR_CHECKS.has(item.operation) && !["audit", "bind_dialogue", "package"].includes(item.operation)).map(item => item.title))]);
const reviewScope = computed(() => [...new Set(stageOperations.value.filter(item => item.review_before && !item.system_required).map(item => item.title))]);
const impactText = computed(() => {
  if (!props.impact) return "";
  const changes = props.impact.changes;
  return Array.isArray(changes) ? `最近修改 ${changes.length} 处` : "查看最近修改影响";
});
async function readUsage(): Promise<void> {
  const run = props.run;
  const project = props.project; const taskId = props.taskId;
  if (!run || !project || !taskId) return;
  const key = contextKey.value; const stamp = generation; const order = ++sequence;
  const runId = run.run.id; const revision = run.run.revision;
  loading.value = true; error.value = "";
  try {
    const result = await storyCreatorClient(project).skillUsage(runId);
    if (!alive || stamp !== generation || order !== sequence || key !== contextKey.value) return;
    if (result.project !== project || result.task_id !== taskId || result.run_id !== runId || result.revision !== revision) throw new Error("Skill 使用记录与本轮运行不一致。");
    usage.value = result;
  } catch (cause) {
    if (alive && stamp === generation && order === sequence && key === contextKey.value) {
      usage.value = null; error.value = humanizeError(cause);
    }
  } finally {
    if (alive && stamp === generation && order === sequence && key === contextKey.value) loading.value = false;
  }
}
watch(refreshKey, () => {
  if (lastContext !== contextKey.value) {
    generation += 1; sequence += 1; lastContext = contextKey.value;
    usage.value = null; error.value = ""; loading.value = false;
  }
  void readUsage();
}, { immediate: true });
onBeforeUnmount(() => { alive = false; generation += 1; sequence += 1; });
</script>

<template>
  <section class="run-overview" aria-label="本轮协作">
    <p v-if="development" class="origin">开发检查</p>
    <div class="turn-line" :class="currentTurn.state" role="status" aria-live="polite" aria-atomic="true"><div><strong>{{ currentTurn.actor }}</strong><p>{{ currentTurn.detail }}</p></div><a v-if="currentTurn.target" :href="currentTurn.target">{{ currentTurn.state === 'waiting' ? '查看待确认内容' : currentTurn.state === 'attention' ? '查看详情' : '查看草稿' }} <span aria-hidden="true">↓</span></a></div>
    <div class="agreement"><span>本轮方式：{{ modeText(run) }}</span><span v-if="run">任务 v{{ run.run.revision }}<template v-if="draftRevision != null && draftRevision !== run.run.revision"> · 当前草稿 v{{ draftRevision }}</template></span><a v-if="impactText" href="#creator-last-impact">{{ impactText }} ↗</a></div>
    <p v-if="summaryIssue" class="summary-warning" role="status">Skill 整理未启动：{{ summaryIssue }}</p>
    <details v-if="stageOperations.length" class="scope"><summary>本轮分工</summary><dl><dt>AI 参与</dt><dd>{{ aiScope.length ? aiScope.join('、') : '未开放创作步骤' }}</dd><dt>执行前确认</dt><dd>{{ reviewScope.length ? reviewScope.join('、') : '按约定连续执行' }}</dd><dt>技术检查</dt><dd>由系统执行</dd></dl></details>
    <details v-if="run" class="used-skills"><summary>本轮 Skill <span v-if="usage && usage.coverage !== 'unavailable'">· {{ usage.skills.length ? `${usage.skills.length} 项` : usage.coverage === 'complete' ? '未选用' : '记录未齐' }}</span></summary>
      <p v-if="loading && !usage" class="hint">正在读取使用记录…</p><p v-else-if="error" class="hint">使用记录暂未读回。<button @click="readUsage">重新读取</button></p>
      <template v-else-if="usage"><p v-if="!usage.skills.length">{{ usage.coverage === 'complete' ? '本轮未选用 Skill。' : '本轮 Skill 记录尚未齐。' }}</p><article v-for="skill in usage.skills" :key="skill.version_id" class="used-skill"><header><h3>{{ skill.name }} <span>{{ skill.version == null ? '版本待读回' : `v${skill.version}` }}</span></h3><span>{{ skill.selection === 'explicit_task' ? '本次选用' : '已采用的版本' }}</span></header><p>{{ usageSummary(skill) }}</p><ol><li v-for="step in skill.steps" :key="step.id"><span>{{ step.title }}</span><span class="step-state">{{ usageStatus(step.status) }}</span><details v-if="step.nodes && step.nodes.length > 1" class="step-nodes"><summary>参与的制作步骤</summary><ul><li v-for="node in step.nodes" :key="node.id">{{ nodeName(node.id) }} · {{ usageStatus(node.status) }}</li></ul></details></li></ol></article><p v-if="usage.skills.length" class="hint">这里记录指导是否带入调用；是否符合创作意图请结合草稿判断。</p><p v-if="usage.coverage === 'partial'" class="hint">部分记录尚未齐，未据此判断未使用的步骤。</p><p v-if="usage.measurements" class="measurements">本轮 {{ usage.measurements.dispatch_count }} 次调用<span v-if="usage.measurements.total_tokens != null"> · {{ usage.measurements.total_tokens.toLocaleString() }} token{{ usage.measurements.usage_complete ? '' : '（部分）' }}</span></p></template>
    </details>
  </section>
</template>

<style scoped>
.run-overview { border: 1px solid var(--ow-paper-edge); border-radius: 8px; margin: 1rem 0; overflow: hidden; }
.origin { margin: .65rem .9rem 0; color: var(--ow-ink-mute); font-size: .69rem; }
.turn-line { display: flex; align-items: center; justify-content: space-between; gap: .8rem; padding: .9rem; border-left: 3px solid var(--ow-ink-mute); }
.turn-line.working { border-left-color: var(--creator-teal, #8bc5b9); }.turn-line.waiting, .turn-line.attention { border-left-color: var(--ow-gold); }
.turn-line strong { font-size: .94rem; font-weight: 600; }.turn-line p { margin: .3rem 0 0; color: var(--ow-ink-soft); font-size: .78rem; line-height: 1.7; }
a, button { color: var(--creator-teal, var(--ow-gold)); font-size: .75rem; white-space: nowrap; text-decoration: none; }a:hover { text-decoration: underline; }a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--ow-gold-soft); outline-offset: 3px; }
.agreement { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem 1rem; padding: 0 .9rem .85rem; font-size: .73rem; color: var(--ow-ink-mute); }
.summary-warning { margin: 0; padding: 0 .9rem .85rem; color: var(--ow-gold); font-size: .76rem; line-height: 1.7; }
.used-skills { border-top: 1px solid var(--ow-paper-edge); padding: .75rem .9rem; }summary { cursor: pointer; color: var(--ow-ink-soft); font-size: .78rem; }summary > span { color: var(--ow-ink-mute); }
.scope { border-top: 1px solid var(--ow-paper-edge); padding: .75rem .9rem; }.scope dl { display: grid; grid-template-columns: auto 1fr; gap: .4rem .8rem; font-size: .76rem; line-height: 1.7; margin: .6rem 0 0; }.scope dt { color: var(--ow-ink-mute); }.scope dd { margin: 0; color: var(--ow-ink-soft); }
.used-skills p { font-size: .77rem; line-height: 1.8; margin: .6rem 0 0; }.hint,.measurements { color: var(--ow-ink-mute); }.used-skill { margin-top: .8rem; padding-top: .5rem; border-top: 1px solid var(--ow-paper-edge); }
.used-skill header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .4rem; align-items: center; }.used-skill h3 { font-size: .81rem; margin: 0; }.used-skill h3 span { color: var(--ow-ink-mute); font-weight: 400; }.used-skill header > span { color: var(--ow-ink-mute); font-size: .68rem; }
ol { margin: .6rem 0 0; padding-left: 1.2rem; }li { padding: .25rem 0; font-size: .76rem; line-height: 1.7; }li > span:first-child { margin-right: .8rem; }.step-state { color: var(--ow-ink-mute); font-size: .71rem; }
.step-nodes { margin-top: .35rem; }.step-nodes summary { font-size: .7rem; }.step-nodes ul { margin: .25rem 0 0; padding-left: 1rem; color: var(--ow-ink-mute); }
button { background: transparent; border: 0; cursor: pointer; padding: .2rem; }
@media (max-width: 600px) { .turn-line { align-items: flex-start; flex-direction: column; }.agreement { gap: .4rem .7rem; } }
</style>
