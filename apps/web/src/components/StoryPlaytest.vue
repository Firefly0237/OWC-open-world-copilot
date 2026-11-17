<script setup lang="ts">
import { computed } from "vue";
import { previewText, type StudioPreview } from "../studioApi";
import { creatorStatus, type StoryQuest, type StoryTree } from "../storyCreatorApi";

const props = defineProps<{ preview: StudioPreview | null; quest?: StoryQuest; tree?: StoryTree; busy: boolean; canStart: boolean; feedbackEnabled?: boolean; stepDisabled?: boolean }>();
const emit = defineEmits<{ start: []; step: [choiceId?: string]; feedback: [] }>();
const names = computed(() => ({ ...Object.fromEntries((props.quest?.logic?.variables ?? []).map((item) => [item.id, item.name || item.id])), ...props.preview?.view.state_names }));
const values = computed(() => Object.entries(props.preview?.view.state ?? {}).filter(([key]) => !key.startsWith("_owc_")));
const changes = computed(() => props.preview?.view.state_changes ?? []);
const stateIds = computed(() => [...new Set([
  ...(props.quest?.logic?.variables ?? []).map((variable) => variable.id),
  ...values.value.map(([key]) => key).sort(),
  ...changes.value.map((change) => change.id).sort(),
])]);
function stateName(id: string): string {
  const name = names.value[id] || changes.value.find((change) => change.id === id)?.name;
  return name && name !== id ? name : `状态 ${stateIds.value.indexOf(id) + 1}`;
}
const history = computed(() => (Array.isArray(props.preview?.view.history) ? props.preview.view.history : [])
  .filter((entry): entry is Record<string, unknown> => entry != null && typeof entry === "object" && !Array.isArray(entry)));
function valueText(value: unknown): string {
  if (value === true) return "是";
  if (value === false) return "否";
  if (value == null) return "未设定";
  if (["string", "number"].includes(typeof value)) return String(value);
  return "复合状态";
}
function stepText(entry: Record<string, unknown>, index: number): string {
  const cursor = typeof entry.cursor === "string" ? entry.cursor : "";
  const line = entry.phase === "dialogue" ? props.tree?.nodes[cursor]?.text : undefined;
  return line?.slice(0, 65) || `场景步骤 ${index + 1}`;
}
</script>

<template>
  <section class="playtest">
    <header><div><span class="eyebrow">PLAYTEST</span><h3>试玩</h3></div><button :disabled="busy || !canStart" @click="emit('start')">{{ preview ? '重新试玩' : '开始试玩' }}</button></header>
    <p v-if="!preview" class="hint">{{ canStart ? '选择选项，查看这条路线的对白和结果。' : '草稿准备好后可试玩。' }}</p>
    <template v-else>
      <span class="status">{{ creatorStatus(preview.view.status, 'preview') }}</span>
      <p v-if="previewText(preview)" class="story-line">{{ previewText(preview) }}</p>
      <p v-if="preview.view.text && preview.view.message && preview.view.message !== preview.view.text" class="hint">{{ preview.view.message }}</p>
      <div class="options"><button v-for="(choice, index) in preview.view.choices" :key="choice.id" :disabled="busy || stepDisabled || choice.enabled === false || choice.available === false" @click="emit('step', choice.id)"><span>{{ index + 1 }}</span><div>{{ choice.label }}<small v-if="choice.reason">{{ choice.reason }}</small></div><b aria-hidden="true">→</b></button><button v-if="preview.view.can_advance && !preview.view.choices.length" :disabled="busy || stepDisabled" @click="emit('step')">继续这条路径 →</button></div>
      <button v-if="feedbackEnabled !== false" class="link-button" :disabled="busy || !preview.view.text" @click="emit('feedback')">修改此处</button>
      <details v-if="values.length || changes.length" class="facts"><summary>剧情状态 <span>{{ values.length }}项</span></summary><dl><template v-for="[key, value] in values" :key="key"><dt>{{ stateName(key) }}</dt><dd>{{ valueText(value) }}</dd></template></dl><template v-if="changes.length"><h4>本步变化</h4><ul><li v-for="change in changes" :key="change.id">{{ stateName(change.id) }}：{{ valueText(change.before) }} → {{ valueText(change.after) }}</li></ul></template><details><summary>状态标识</summary><ul><li v-for="[key] in values" :key="key">{{ stateName(key) }} · {{ key }}</li></ul></details></details>
      <details v-if="history.length"><summary>试玩路线 · {{ history.length }}步</summary><ol><li v-for="(entry, index) in history" :key="index">{{ stepText(entry, index) }}</li></ol></details>
    </template>
  </section>
</template>

<style scoped>
.playtest { display: grid; gap: 1rem; }
header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
h3 { font-size: 1.05rem; margin: .25rem 0 0; }
.eyebrow { font-size: .67rem; letter-spacing: .22em; color: var(--ow-gold); }
.hint { font-size: .85rem; color: var(--ow-ink-mute); line-height: 1.8; margin: 0; }
.status { justify-self: start; font-size: .74rem; color: var(--ow-cyan); }
.story-line { font-family: var(--ow-serif); font-size: 1.08rem; line-height: 2; white-space: pre-wrap; margin: .3rem 0; }
button { color: inherit; font: inherit; cursor: pointer; background: rgba(255,255,255,.025); border: 1px solid var(--ow-paper-edge); border-radius: 7px; padding: .6rem .8rem; }
button:disabled { opacity: .45; cursor: default; }
.options { display: grid; gap: .6rem; }
.options button { display: flex; text-align: left; align-items: center; gap: .8rem; line-height: 1.7; padding: .85rem; }
.options button:not(:disabled):hover { background: var(--ow-gold-faint); border-color: var(--ow-gold); }
.options button > span { color: var(--ow-gold); font: .8rem var(--ow-num); }
.options button > div { flex: 1; }
.options small { display: block; color: var(--ow-ink-mute); font-size: .75rem; }
.options b { color: var(--ow-gold); }
.link-button { padding: 0; background: transparent; border: none; justify-self: start; font-size: .8rem; color: var(--ow-cyan); }
details { border-top: 1px solid var(--ow-paper-edge); padding-top: .8rem; }
summary { cursor: pointer; font-size: .83rem; color: var(--ow-ink-soft); }
summary span { margin-left: .5rem; color: var(--ow-ink-mute); font-size: .72rem; }
dl { display: grid; grid-template-columns: 1fr auto; gap: .55rem 1rem; font-size: .8rem; }
dt { color: var(--ow-ink-soft); overflow-wrap: anywhere; } dd { margin: 0; color: var(--ow-cyan); }
ol { padding-left: 1.2rem; font-size: .78rem; color: var(--ow-ink-mute); line-height: 1.9; }
</style>
