<script setup lang="ts">
import { computed } from "vue";
import { CREATOR_OPERATIONS } from "../storyCreatorApi";

const props = defineProps<{ impact?: Record<string, unknown> | null; fieldLabel?: string; phase?: "before" | "after" }>();

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function rows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item) => item != null && typeof item === "object" && !Array.isArray(item)) : [];
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
const changes = computed(() => rows(props.impact?.changes));
const changesAvailable = computed(() => Array.isArray(props.impact?.changes));
const production = computed(() => record(props.impact?.production));
const affectedOperations = computed(() => strings(production.value.affected_operations));
const staleArtifactCount = computed(() => {
  const values = props.phase === "before" ? production.value.newly_stale_artifacts : production.value.stale_artifacts;
  return Array.isArray(values) ? values.length : null;
});
const scope = computed(() => record(props.impact?.scope));
const hardLocks = computed(() => rows(scope.value.hard_locks));
const references = computed(() => record(props.impact?.references));
const retainedReferenceIds = computed(() => strings(references.value.retained_reference_ids));
const reading = computed(() => record(props.impact?.reading));
const playtest = computed(() => record(props.impact?.playtest));
const unconfirmed = computed(() => rows(props.impact?.unconfirmed)
  .filter((item) => typeof item.text === "string" && item.text.trim()));
const modificationGoal = computed(() => record(props.impact?.reason).text);
const readingState = computed(() => {
  if (typeof reading.value.path !== "string" || !reading.value.path) return "未关联原阅读位置";
  return reading.value.target_exists === true ? (props.phase === "before" ? "应用后仍可定位原字段" : "新版中可定位原字段")
    : reading.value.target_exists === false ? (props.phase === "before" ? "应用后原字段将不存在" : "原字段在新版中不存在") : "原位置尚未核对";
});
const playtestState = computed(() => {
  const labels: Record<string, string> = {
    compatible: "可选择继承原试玩", incompatible: "原试玩暂不能继承",
    needs_binding: "需先绑定新版场景", eligible_for_check: "待检查原试玩兼容性",
    not_requested: "未关联试玩记录",
  };
  const status = playtest.value.status;
  return typeof status === "string" && typeof labels[status] === "string"
    ? labels[status] : "试玩兼容性尚未核对";
});
function valueText(change: Record<string, unknown>, side: "before" | "after"): string {
  if (change[`${side}_exists`] === false) return "字段不存在";
  if (!(side in change)) return "未返回内容";
  const value = change[side];
  if (typeof value === "string") return value || "（空文本）";
  if (value === null) return "空值";
  if (value === true) return "是";
  if (value === false) return "否";
  return JSON.stringify(value, null, 2) ?? "未返回内容";
}
function operationLabel(operation: string): string {
  return typeof CREATOR_OPERATIONS[operation] === "string" ? CREATOR_OPERATIONS[operation] : "其他制作步骤";
}
function detailsText(value: unknown): string { return JSON.stringify(value, null, 2) ?? "未提供"; }
</script>

<template>
  <section class="impact-summary">
    <h3>{{ phase === 'before' ? '应用前影响' : '修改影响' }}</h3>
    <p v-if="!impact" class="hint">尚未取得影响记录。</p>
    <template v-else>
      <p v-if="typeof modificationGoal === 'string' && modificationGoal" class="goal"><span>修改目标</span>{{ modificationGoal }}</p>
      <p class="changed-field">{{ fieldLabel || '内容改动' }} · {{ changesAvailable ? `${changes.length}处` : '尚未核对' }}</p>
      <details v-if="changes.length" class="text-changes">
        <summary>查看修改前后</summary>
        <article v-for="(change, index) in changes" :key="typeof change.path === 'string' ? change.path : index">
          <h4 v-if="changes.length > 1">改动{{ index + 1 }}</h4>
          <div class="comparison"><div><h4>修改前</h4><p>{{ valueText(change, 'before') }}</p></div><div><h4>修改后</h4><p>{{ valueText(change, 'after') }}</p></div></div>
        </article>
      </details>
      <p v-else class="hint">{{ changesAvailable ? '记录中没有字段改动。' : '未返回具体改动字段。' }}</p>
      <dl class="impact-status">
        <dt v-if="phase === 'before'">保护约定</dt><dd v-if="phase === 'before'">{{ hardLocks.length ? `保留${hardLocks.length}项锁定范围` : '本次未另设字段锁定' }}</dd>
        <dt>阅读位置</dt><dd>{{ readingState }}<small v-if="typeof reading.path === 'string' && reading.path && reading.target_exists === true && reading.current_text_will_change === true">{{ phase === 'before' ? '当前位置的文字将改变' : '当前位置的文字已改变' }}</small></dd>
        <dt>继续试玩</dt><dd>{{ playtestState }}</dd>
        <dt>制作内容</dt><dd><template v-if="affectedOperations.length">受影响步骤：{{ affectedOperations.map(operationLabel).join('、') }}</template><template v-else>未列出受影响的制作步骤</template><small v-if="production.basis !== 'stored_production_graph'">制作依赖尚未完整关联</small></dd>
        <dt>旧产物</dt><dd>{{ staleArtifactCount !== null ? `${staleArtifactCount}份${phase === 'before' ? '将失效' : '已失效'}` : '失效范围尚未核对' }}</dd>
        <dt v-if="phase === 'before'">资料与后续剧情</dt><dd v-if="phase === 'before'">{{ retainedReferenceIds.length ? `保留${retainedReferenceIds.length}份所选资料` : '本次未关联参考资料' }}<small>跨资料与跨场景的意义影响尚未判定</small></dd>
      </dl>
      <ul v-if="strings(playtest.reasons).length" class="reasons"><li v-for="reason in strings(playtest.reasons)" :key="reason">{{ reason }}</li></ul>
      <section v-if="unconfirmed.length" class="unconfirmed"><h4>待你判断</h4><ul><li v-for="(item, index) in unconfirmed" :key="index">{{ item.text }}</li></ul></section>
      <p v-else class="hint">未提供意义与文风方面的判断。</p>
      <details class="evidence"><summary>查看影响依据</summary><dl><dt>制作依赖来源</dt><dd>{{ production.basis === 'stored_production_graph' ? '原制作流程' : '尚未关联' }}</dd></dl><ul v-if="strings(production.unknown_reasons).length"><li v-for="reason in strings(production.unknown_reasons)" :key="reason">{{ reason }}</li></ul><pre>{{ detailsText(impact) }}</pre></details>
    </template>
  </section>
</template>

<style scoped>
.impact-summary { display: grid; gap: .7rem; padding: .9rem; border: 1px solid var(--ow-paper-edge); border-radius: 7px; min-width: 0; }
h3,h4,p { margin: 0; }h3 { font-size: .94rem; }h4 { font-size: .77rem; font-weight: 600; }
.hint,.goal,.changed-field,.impact-status,.reasons,.unconfirmed,.evidence { font-size: .78rem; line-height: 1.8; }
.hint { color: var(--ow-ink-mute); }.goal { color: var(--ow-ink-soft); white-space: pre-wrap; overflow-wrap: anywhere; }.goal span { color: var(--ow-gold); display: block; font-size: .7rem; }
.changed-field { color: var(--ow-gold); }summary { cursor: pointer; color: var(--ow-ink-soft); font-size: .77rem; }
.text-changes article { margin-top: .8rem; }.comparison { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: .45rem; }.comparison > div { padding: .65rem; background: rgba(0,0,0,.08); border: 1px solid var(--ow-paper-edge); border-radius: 5px; min-width: 0; }.comparison h4 { color: var(--ow-ink-mute); }.comparison p { font-family: var(--ow-serif); font-size: .93rem; line-height: 1.9; white-space: pre-wrap; overflow-wrap: anywhere; margin-top: .3rem; }
.impact-status { display: grid; grid-template-columns: auto 1fr; gap: .4rem 1rem; margin: 0; }.impact-status dt { color: var(--ow-ink-mute); }.impact-status dd { margin: 0; overflow-wrap: anywhere; }.impact-status small { display: block; color: var(--ow-ink-mute); font-size: .72rem; }
ul { margin: .35rem 0 0; padding-left: 1.15rem; }.unconfirmed { border-left: 2px solid var(--ow-gold-soft); padding-left: .7rem; }.evidence { border-top: 1px solid var(--ow-paper-edge); padding-top: .6rem; }.evidence dl { margin: .65rem 0; }.evidence dd { margin-left: 0; }.evidence pre { margin: .5rem 0 0; font-size: .68rem; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 20rem; overflow: auto; }
@media (max-width: 700px) { .comparison { grid-template-columns: 1fr; }.impact-status { gap: .4rem .65rem; } }
</style>
