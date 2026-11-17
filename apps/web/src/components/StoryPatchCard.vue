<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { humanizeError } from "../api";
import { creatorStatus, storyCreatorClient, type AIPatchReview, type ApplicationContinuation, type CreatorJob, type PatchSelection } from "../storyCreatorApi";
import type { EvolutionArtifact, IntentApplicationReceipt, IntentDecision } from "../evolutionStudioApi";
import type { StudioTask } from "../studioApi";
import StoryImpactSummary from "./StoryImpactSummary.vue";

const props = defineProps<{ project: string; taskId: string; revision: number; source: { artifact_id: string; artifact_hash: string } | null; selection: PatchSelection | null; goal: string; locked: boolean; actorKind?: "author" | "development_operator"; continuation?: ApplicationContinuation }>();
const emit = defineEmits<{
  working: [value: boolean];
  applied: [value: { artifact: EvolutionArtifact; task: StudioTask; receipt: IntentApplicationReceipt }];
}>();
let api = storyCreatorClient(props.project);
let epoch = 0;
let alive = true;
let timer: ReturnType<typeof setTimeout> | undefined;
const busy = ref(false);
const error = ref("");
const notice = ref("");
const job = ref<CreatorJob | null>(null);
const jobId = ref("");
const snapshot = ref<PatchSelection | null>(null);
const sourceRevision = ref(0);
const proposalGoal = ref("");
const review = ref<AIPatchReview | null>(null);
const decision = ref<IntentDecision | null>(null);
const receipt = ref<IntentApplicationReceipt | null>(null);
const pending = computed(() => Boolean(jobId.value) && ["queued", "running"].includes(job.value?.status ?? "queued"));
const current = computed(() => sourceRevision.value === props.revision && Boolean(snapshot.value)
  && snapshot.value?.artifact_id === props.source?.artifact_id && snapshot.value?.artifact_hash === props.source?.artifact_hash);

function readPointer(document: unknown, pointer: string): unknown {
  let value = document;
  for (const token of pointer.slice(1).split("/").map((item) => item.replaceAll("~1", "/").replaceAll("~0", "~"))) {
    if (value == null || typeof value !== "object") return undefined;
    value = (value as Record<string, unknown>)[token];
  }
  return value;
}
const proposedText = computed<string | null>(() => {
  if (!review.value || !snapshot.value) return null;
  const fromShadow = readPointer(review.value.report.candidate_bundle, snapshot.value.bundle_path);
  if (typeof fromShadow === "string") return fromShadow;
  const replace = review.value.candidate.ops.filter((operation) => operation.op === "replace" && operation.path === snapshot.value?.bundle_path).at(-1);
  return typeof replace?.value === "string" ? replace.value : null;
});
const diff = computed(() => {
  const before = snapshot.value?.before_text ?? "";
  const after = proposedText.value;
  if (after == null) return null;
  const oldPoints = Array.from(before); const nextPoints = Array.from(after);
  let prefix = 0;
  while (prefix < oldPoints.length && prefix < nextPoints.length && oldPoints[prefix] === nextPoints[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < oldPoints.length - prefix && suffix < nextPoints.length - prefix && oldPoints[oldPoints.length - 1 - suffix] === nextPoints[nextPoints.length - 1 - suffix]) suffix += 1;
  return {
    prefix: oldPoints.slice(0, prefix).join(""), suffix: suffix ? oldPoints.slice(-suffix).join("") : "",
    removed: oldPoints.slice(prefix, oldPoints.length - suffix).join(""),
    added: nextPoints.slice(prefix, nextPoints.length - suffix).join(""),
  };
});
function stop(): void { if (timer) clearTimeout(timer); timer = undefined; }
function working(value: boolean): void { busy.value = value; emit("working", value); }
function reset(): void {
  epoch += 1; stop(); busy.value = false; emit("working", false); error.value = ""; notice.value = "";
  job.value = null; jobId.value = ""; review.value = null; decision.value = null; receipt.value = null; snapshot.value = null; sourceRevision.value = 0; proposalGoal.value = "";
}
function isReview(value: unknown): value is AIPatchReview {
  if (value == null || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  if (typeof record.id !== "string" || typeof record.review_hash !== "string" || typeof record.task_revision !== "number"
    || typeof record.baseline_artifact_id !== "string" || typeof record.baseline_artifact_hash !== "string") return false;
  const report = record.report as Record<string, unknown> | undefined;
  const candidate = record.candidate as Record<string, unknown> | undefined;
  return Boolean(report && candidate && Array.isArray(candidate.ops) && typeof report.status === "string"
    && Array.isArray(report.reasons) && Array.isArray(report.changed_paths) && Array.isArray(report.formal_results)
    && Array.isArray(report.introduced_errors) && Array.isArray(report.remaining_targets));
}
function formalState(result: Record<string, unknown>, side: "before" | "after"): string {
  const observation = result[side];
  if (observation == null || typeof observation !== "object" || Array.isArray(observation)) return "没有返回检查结果";
  const fields = observation as Record<string, unknown>;
  return [creatorStatus(typeof fields.status === "string" ? fields.status : undefined),
    fields.strong === true ? "强检查" : "不构成强证明", typeof fields.reason === "string" ? fields.reason : ""].filter(Boolean).join("；");
}
async function poll(): Promise<void> {
  if (!jobId.value) return;
  const stamp = epoch; const id = jobId.value;
  const state = await api.job(id);
  if (!alive || stamp !== epoch || id !== jobId.value) return;
  job.value = state; stop();
  if (["queued", "running"].includes(state.status)) {
    timer = setTimeout(() => { void poll().catch((failure) => {
      if (alive && stamp === epoch) { stop(); working(false); error.value = `自动读取已停止：${humanizeError(failure)}。可手动刷新建议。`; }
    }); }, 2500);
    return;
  }
  working(false);
  if (state.status === "failed") { error.value = state.error || "修改建议未生成，原稿保留。"; return; }
  if (!isReview(state.result)) { error.value = "未取得完整的修改审查记录，原稿保留。"; return; }
  if (state.result.baseline_artifact_id !== snapshot.value?.artifact_id || state.result.baseline_artifact_hash !== snapshot.value?.artifact_hash || state.result.task_revision !== sourceRevision.value) {
    error.value = "建议对应的草稿或授权版本已变，不能应用。"; return;
  }
  review.value = state.result; decision.value = state.result.author_decision ?? null;
  notice.value = "修改建议已就绪，原稿尚未改变。";
}
async function refreshSuggestion(): Promise<void> {
  const stamp = epoch;
  try { await poll(); } catch (failure) { if (alive && stamp === epoch) error.value = humanizeError(failure); }
}
async function rereadApplied(): Promise<void> {
  const stamp = epoch;
  error.value = "";
  try { await readApplied(); } catch (failure) { if (alive && stamp === epoch) error.value = humanizeError(failure); }
}
async function propose(): Promise<void> {
  const selected = props.selection;
  if (!selected || !props.goal.trim() || props.locked || busy.value || pending.value) return;
  reset(); snapshot.value = { ...selected }; sourceRevision.value = props.revision;
  working(true); const stamp = epoch; const goal = props.goal.trim();
  proposalGoal.value = goal;
  try {
    const history = (await api.intentHistory(props.taskId)).contracts;
    if (!alive || stamp !== epoch) return;
    const latest = history.at(-1);
    const confirmed = [...history].reverse().find((item) => item.contract.confirmed);
    const proposal = await api.finiteIntent(props.taskId, {
      expected_revision: sourceRevision.value, baseline_artifact_id: selected.artifact_id, baseline_artifact_hash: selected.artifact_hash,
      allowed_paths: [selected.bundle_path], hard_locks: confirmed?.contract.hard_locks ?? [],
      formal_rules: confirmed?.contract.formal_rules ?? [], soft_text: confirmed?.contract.soft_text ?? [],
      target_error_fingerprints: confirmed?.contract.target_error_fingerprints ?? [], modification_goal: goal,
      ...(latest ? { supersedes_contract_id: latest.contract.id, expected_parent_hash: latest.contract_hash,
        change_reason: `本次局部修改：${goal}` } : {}),
    });
    if (!alive || stamp !== epoch) return;
    // This button explicitly grants only this field's modification scope. It does
    // not accept AI content; the later candidate decision is a separate action.
    const confirmedScope = await api.confirmIntent(proposal.contract.id, sourceRevision.value, proposal.contract_hash, props.actorKind ?? "author");
    if (!alive || stamp !== epoch) return;
    const submission = await api.proposePatch(confirmedScope.contract.id, sourceRevision.value, confirmedScope.contract_hash);
    if (!alive || stamp !== epoch) return;
    jobId.value = submission.job_id; job.value = { job_id: submission.job_id, kind: "intent_patch", status: "queued", events: [] };
    notice.value = "正在生成修改建议。原稿保留。"; await poll();
  } catch (failure) { if (alive && stamp === epoch) { working(false); error.value = humanizeError(failure); } }
}
async function decide(value: "accepted" | "rejected"): Promise<void> {
  if (!review.value || !current.value || busy.value || props.locked) return;
  working(true); error.value = ""; const stamp = epoch;
  try {
    const result = await api.decideIntent(review.value.id, sourceRevision.value, review.value.review_hash, value, props.actorKind ?? "author");
    if (!alive || stamp !== epoch) return;
    decision.value = result; notice.value = value === "accepted" ? "已选用，尚未应用。" : "未采用，原稿保留。";
  } catch (failure) { if (alive && stamp === epoch) error.value = humanizeError(failure); }
  finally { if (alive && stamp === epoch) working(false); }
}
async function readApplied(): Promise<void> {
  if (!receipt.value) return;
  const stamp = epoch; const receiptId = receipt.value.id;
  const detail = await api.application(receiptId);
  if (!alive || stamp !== epoch || receipt.value?.id !== receiptId) return;
  if (detail.receipt.id !== receiptId) throw new Error("读取的回执与本次修改不一致。");
  receipt.value = detail.receipt;
  if (detail.target_current !== true) {
    notice.value = detail.target_current === false ? "此回执对应历史版本，当前草稿保持不变。" : "尚未核对当前草稿版本，请重新读取回执。";
    return;
  }
  emit("applied", { artifact: detail.artifact, task: detail.task, receipt: detail.receipt });
  notice.value = "已载入修改后的草稿。";
}
async function apply(): Promise<void> {
  if (!review.value || decision.value?.decision !== "accepted" || !current.value || busy.value || props.locked || receipt.value) return;
  working(true); error.value = ""; const stamp = epoch;
  try {
    const result = await api.applyIntent(review.value.id, review.value.review_hash, decision.value.decision_hash, props.continuation);
    if (!alive || stamp !== epoch) return;
    receipt.value = result; await readApplied();
  } catch (failure) {
    if (alive && stamp === epoch) error.value = `${receipt.value ? "应用回执已保存，但读取新稿未完成：" : "应用未完成："}${humanizeError(failure)}`;
  } finally { if (alive && stamp === epoch) working(false); }
}
watch(() => `${props.project}:${props.taskId}`, () => { reset(); api = storyCreatorClient(props.project); });
watch(() => props.selection ? `${props.selection.artifact_id}:${props.selection.bundle_path}` : "", (value, previous) => {
  if (value && value !== previous) reset();
});
onUnmounted(() => { alive = false; epoch += 1; stop(); emit("working", false); });
</script>

<template>
  <section v-if="selection || snapshot || review" class="patch-card">
    <header><div><span class="eyebrow">FOCUSED REVISION</span><h3>修改建议</h3></div><span v-if="pending" class="phase">{{ creatorStatus(job?.status) }}</span></header>
    <p class="scope-copy">{{ selection?.label || snapshot?.label }}。生成后可查看前后对照，再决定是否应用。</p>
    <p v-if="snapshot" class="submitted-goal">修改目标：{{ proposalGoal }}</p>
    <button class="primary" :disabled="locked || busy || pending || !selection || !goal.trim()" @click="propose">生成此处的修改建议</button>
    <p v-if="!goal.trim() && !snapshot" class="hint">先填写上方的修改意见。</p>
    <p v-if="error" class="message error" role="alert">{{ error }}</p><p v-if="notice" class="message notice" role="status">{{ notice }}</p>
    <button v-if="jobId" class="text-link" :disabled="busy && !pending" @click="refreshSuggestion">刷新建议</button>
    <template v-if="review && snapshot">
      <div class="text-comparison"><article><span>修改前</span><p>{{ snapshot.before_text }}</p></article><article><span>修改建议</span><p v-if="proposedText !== null">{{ proposedText }}</p><p v-else class="hint">这份建议没有可展示的文本修改，暂不能选用。</p></article></div>
      <details v-if="diff"><summary>查看文字差异</summary><p class="inline-diff">{{ diff.prefix }}<del v-if="diff.removed">{{ diff.removed }}</del><ins v-if="diff.added">{{ diff.added }}</ins>{{ diff.suffix }}</p></details>
      <div class="hard-checks"><strong>{{ creatorStatus(review.report.status, "review") }}</strong><ul v-if="review.report.reasons.length"><li v-for="reason in review.report.reasons" :key="reason">{{ reason }}</li></ul><p>变更{{ review.report.changed_paths.length }}处 · 新增错误{{ review.report.introduced_errors.length }}项 · 目标问题未解决{{ review.report.remaining_targets.length }}项</p><p class="hint">是否符合修改意图：{{ creatorStatus(review.report.goal_status, "requirements") }}</p><details v-if="review.report.formal_results.length || review.report.soft_results.length"><summary>条件检查与待确认项</summary><div v-for="(result, index) in review.report.formal_results" :key="index" class="formal-result"><p>条件 {{ index + 1 }} · 修改前：{{ formalState(result, 'before') }}</p><p>修改后：{{ formalState(result, 'after') }}</p></div><p v-for="(result, index) in review.report.soft_results" :key="index">{{ result.text }} · {{ creatorStatus(result.status, "requirements") }}</p></details></div>
      <p v-if="!current && !receipt" class="hint">源草稿已更新。此建议仅供回看，请针对当前版本重新生成。</p>
      <div class="actions"><button :disabled="locked || busy || !current || Boolean(decision) || proposedText === null || review.report.status !== 'ready_for_author'" @click="decide('accepted')">选用此建议</button><button :disabled="locked || busy || !current || Boolean(decision)" @click="decide('rejected')">不采用</button><button v-if="decision?.decision === 'accepted'" class="primary" :disabled="locked || busy || !current || Boolean(receipt)" @click="apply">应用本次修改</button></div>
      <p v-if="decision" class="hint">{{ actorKind === 'development_operator' ? '开发操作 · ' : '' }}{{ creatorStatus(decision.decision, "author") }}{{ decision.decision === 'accepted' && !receipt ? ' · 尚未应用' : '' }}</p><p v-if="receipt" class="applied-note">已保存草稿 v{{ receipt.revision }}。<button class="text-link" :disabled="busy" @click="rereadApplied">读取该版本</button></p>
      <StoryImpactSummary v-if="receipt" :impact="receipt.impact" :field-label="snapshot.label" />
      <details class="debug"><summary>检查与操作记录</summary><pre>{{ JSON.stringify({ path: snapshot.bundle_path, review_id: review.id, proposal_trace_run_id: review.proposal_trace_run_id, report: review.report, receipt }, null, 2) }}</pre></details>
    </template>
  </section>
</template>

<style scoped>
.patch-card { margin: 1rem 0; padding: 1.1rem; border: 1px solid var(--ow-gold-soft); background: var(--ow-gold-faint); border-radius: 8px; }header { display: flex; justify-content: space-between; align-items: center; gap: .7rem; }h3 { margin: .3rem 0; font-size: .98rem; }.eyebrow { color: var(--ow-gold); font-size: .62rem; letter-spacing: .15em; }.phase { font-size: .7rem; color: var(--ow-cyan); }.scope-copy,.hint { color: var(--ow-ink-mute); font-size: .74rem; line-height: 1.85; }.scope-copy { color: var(--ow-ink-soft); }.submitted-goal { color: var(--ow-ink-soft); font-size: .77rem; line-height: 1.85; border-left: 2px solid var(--ow-gold-soft); padding-left: .7rem; }
button { color: inherit; font: inherit; font-size: .76rem; padding: .65rem .85rem; border: 1px solid var(--ow-paper-edge); border-radius: 6px; background: rgba(255,255,255,.025); cursor: pointer; }button:disabled { opacity: .42; cursor: default; }.primary { background: var(--ow-gold); color: var(--ow-night-deep); font-weight: 600; border-color: var(--ow-gold); }.actions { display: flex; flex-wrap: wrap; gap: .6rem; }.text-link { border: 0; background: none; padding: .2rem 0; color: var(--ow-cyan); font-size: .7rem; }.message { line-height: 1.85; font-size: .78rem; padding: .6rem .7rem; border-radius: 5px; }.error { color: #eab09f; background: rgba(200,85,61,.08); }.notice,.applied-note { color: #b9d7c7; font-size: .77rem; line-height: 1.85; }
.text-comparison { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin: 1rem 0; }.text-comparison article { padding: .9rem; border: 1px solid var(--ow-paper-edge); border-radius: 6px; background: rgba(0,0,0,.07); min-width: 0; }.text-comparison span { color: var(--ow-gold); font-size: .68rem; }.text-comparison p,.inline-diff { font-family: var(--ow-serif); line-height: 1.95; white-space: pre-wrap; overflow-wrap: anywhere; font-size: .94rem; }.text-comparison p { margin-bottom: 0; }del { background: rgba(200,85,61,.16); color: #eab09f; }ins { background: rgba(112,172,138,.16); color: #c3dec8; text-decoration: none; }.hard-checks { border-top: 1px solid var(--ow-paper-edge); border-bottom: 1px solid var(--ow-paper-edge); margin: .8rem 0 1rem; padding: .8rem 0; }.hard-checks > strong { font-size: .78rem; color: var(--ow-gold); }.hard-checks p,.hard-checks li { font-size: .74rem; line-height: 1.85; }.hard-checks ul { padding-left: 1.1rem; }.formal-result { border-bottom: 1px solid var(--ow-paper-edge); }summary { cursor: pointer; font-size: .74rem; color: var(--ow-ink-soft); }details { margin-top: .8rem; }pre { font-size: .67rem; line-height: 1.7; overflow-wrap: anywhere; white-space: pre-wrap; max-height: 22rem; overflow: auto; background: rgba(0,0,0,.15); padding: .8rem; }
@media(max-width: 650px) { .text-comparison { grid-template-columns: minmax(0,1fr); } }
</style>
