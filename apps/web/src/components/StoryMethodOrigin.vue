<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { humanizeError } from "../api";
import { storyCreatorClient } from "../storyCreatorApi";
import type { MethodEpisode, MethodTrial } from "../evolutionStudioApi";

const props = defineProps<{ project: string; familyId: string; versionId: string }>();
const episodes = ref<MethodEpisode[]>([]);
const loading = ref(false);
const error = ref("");
const complete = ref(false);
let epoch = 0;
let alive = true;
const matchingEpisodes = computed(() => episodes.value.filter((episode) =>
  episode.result?.candidate_version_id === props.versionId || episode.original_result?.candidate_version_id === props.versionId));

async function load(): Promise<void> {
  const stamp = ++epoch;
  const project = props.project; const family = props.familyId; const version = props.versionId;
  episodes.value = []; error.value = ""; complete.value = false;
  if (!project || !family || !version) { loading.value = false; error.value = "版本信息不完整，暂不能读取来源。"; return; }
  loading.value = true;
  try {
    const result = await storyCreatorClient(project).episodes(family);
    if (!alive || stamp !== epoch || project !== props.project || family !== props.familyId || version !== props.versionId) return;
    episodes.value = result.episodes; complete.value = true;
  } catch (failure) {
    if (alive && stamp === epoch && project === props.project && family === props.familyId && version === props.versionId) error.value = `来源记录读取失败：${humanizeError(failure)}`;
  } finally {
    if (alive && stamp === epoch) loading.value = false;
  }
}
function sourceLabel(kind: string | undefined): string {
  return kind === "author_feedback" ? "作者反馈" : kind === "development_input" ? "开发输入" : "来源类型未标注";
}
function trialsFor(episode: MethodEpisode): MethodTrial[] {
  return (episode.trials ?? []).filter((trial) => trial.candidate_version_id === props.versionId);
}
function trialStatus(status: string): string {
  const labels: Record<string, string> = {
    claimed: "已登记，尚无执行结果", claimed_unknown: "执行结果尚未确认", queued: "等待开始",
    running: "试用中", waiting_user: "等待确认", paused: "已暂停", succeeded: "运行已结束",
    completed: "运行已结束", done: "运行已结束", completed_with_skips: "部分完成，仍有未完成步骤", failed: "运行失败",
    failed_setup: "配置未就绪", blocked: "执行条件未满足", interrupted: "已中断", cancelled: "已停止",
    budget_exhausted: "已达到调用上限",
  };
  return labels[status] ?? "状态未识别，详见原始记录";
}
function executionMode(trial: MethodTrial): string {
  const mode = trial.execution_observation?.mode;
  if (mode === "live_generation") return "已记录实际模型响应";
  if (mode === "stored_ir_recompilation_no_model_dispatch") return "由已有内容重新编译，未调用模型";
  return "执行来源尚未分类";
}
function comparisonState(trial: MethodTrial): string {
  const comparison = trial.comparison;
  if (!comparison) return "未关联比较结果";
  if (comparison.status === "comparable" && comparison.same_frozen_input === true) return "已记录同条件技术比较";
  if (comparison.status === "withheld") return "暂不能作同条件比较";
  return "比较状态详见原始记录";
}
function acceptanceState(trial: MethodTrial): string {
  return trial.author_acceptance === "accepted" ? "记录为已采用"
    : trial.author_acceptance === "rejected" ? "记录为未采用" : "本记录未确认是否采用";
}
watch(() => [props.project, props.familyId, props.versionId], () => { void load(); }, { immediate: true });
onUnmounted(() => { alive = false; epoch += 1; });
</script>

<template>
  <section class="method-origin" :aria-busy="loading">
    <header><h3>这版方法的来源</h3><button :disabled="loading" @click="load">刷新来源</button></header>
    <p v-if="loading" class="hint" role="status">正在读取提议与试用记录…</p>
    <p v-else-if="error" class="error" role="alert">{{ error }}</p>
    <p v-else-if="complete && !matchingEpisodes.length" class="hint">此版本没有关联的改进提议记录。</p>
    <article v-for="episode in matchingEpisodes" :key="episode.id" class="episode">
      <span class="source-kind">{{ sourceLabel(episode.source?.source_kind) }}</span>
      <p v-if="episode.source?.text" class="source-text">{{ episode.source.text }}</p>
      <p v-else class="hint">记录中没有反馈原文。</p>
      <p class="hint">{{ episode.parent_version_id ? '已关联父版本' : '未提供父版本' }} · {{ episode.source?.run_id ? '已关联来源运行' : '未提供来源运行' }}</p>
      <p v-if="!trialsFor(episode).length" class="hint">这份提议尚未关联该版本的试用结果。</p>
      <section v-for="(trial, index) in trialsFor(episode)" :key="trial.trial_key || trial.run_id || index" class="trial">
        <h4>试用{{ index + 1 }} · {{ trialStatus(trial.status) }}</h4>
        <p class="hint">{{ executionMode(trial) }} · {{ comparisonState(trial) }}</p>
        <p class="hint">{{ acceptanceState(trial) }}</p>
        <p v-if="trial.comparison?.scope === 'technical_observation_not_causal_quality_or_generalization'" class="hint">比较仅涉及本次执行的技术记录。</p>
        <details><summary>查看本次试用记录</summary><pre>{{ JSON.stringify(trial, null, 2) }}</pre></details>
      </section>
      <details class="source-record"><summary>父版本、来源运行与提议记录</summary><dl><dt>父版本</dt><dd>{{ episode.parent_version_id || '未提供' }}</dd><dt>来源运行</dt><dd>{{ episode.source?.run_id || '未提供' }}</dd></dl><pre>{{ JSON.stringify(episode, null, 2) }}</pre></details>
    </article>
  </section>
</template>

<style scoped>
.method-origin { border-top: 1px solid var(--ow-paper-edge); padding-top: .8rem; margin-top: .8rem; min-width: 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: .7rem; }h3,h4 { margin: 0; }h3 { font-size: .84rem; }h4 { font-size: .77rem; font-weight: 600; }
button { border: 0; padding: .25rem 0; color: var(--ow-cyan); background: none; font: inherit; font-size: .71rem; cursor: pointer; }button:disabled { opacity: .45; cursor: default; }
.hint,.error { font-size: .75rem; line-height: 1.8; margin: .45rem 0; }.hint { color: var(--ow-ink-mute); }.error { color: #eab09f; overflow-wrap: anywhere; }
.episode { border-top: 1px solid var(--ow-paper-edge); padding-top: .7rem; margin-top: .7rem; }.source-kind { color: var(--ow-gold); font-size: .7rem; }.source-text { color: var(--ow-ink-soft); font-size: .82rem; line-height: 1.9; white-space: pre-wrap; overflow-wrap: anywhere; margin: .45rem 0; }
.trial { margin-top: .7rem; padding: .6rem .7rem; border: 1px solid var(--ow-paper-edge); border-radius: 5px; }details { margin-top: .6rem; }summary { color: var(--ow-ink-soft); font-size: .73rem; cursor: pointer; }dl { display: grid; grid-template-columns: auto 1fr; gap: .4rem .8rem; font-size: .71rem; line-height: 1.8; }dt { color: var(--ow-ink-mute); }dd { margin: 0; overflow-wrap: anywhere; }pre { max-height: 20rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: .67rem; line-height: 1.7; color: var(--ow-ink-mute); }
</style>
