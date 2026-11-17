<script setup lang="ts">
import { computed, useId } from "vue";
import { CREATOR_OPERATIONS, type CollaborationPolicy } from "../storyCreatorApi";

const props = defineProps<{
  modelValue: CollaborationPolicy;
  operations?: { operation: string; label?: string }[];
  disabled?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [value: CollaborationPolicy] }>();
const id = useId();
const operationIds = [
  "plan_events", "draft_quest", "draft_logic", "logic_contract", "draft_dialogue",
  "dialogue_constrained", "bind_dialogue", "coverage_audit", "validate_decisions",
];
const registered = new Set(operationIds);
const visibleOperations = computed(() => {
  const seen = new Set<string>();
  const choices: { operation: string; label?: string }[] = props.operations
    ?? operationIds.map(operation => ({ operation }));
  const visible: { operation: string; label: string }[] = [];
  for (const item of choices) {
    if (!registered.has(item.operation) || seen.has(item.operation)) continue;
    seen.add(item.operation);
    visible.push({ operation: item.operation, label: item.label || CREATOR_OPERATIONS[item.operation] });
  }
  return visible;
});
function allowed(operation: string): boolean {
  return props.modelValue.allowed_operations == null || props.modelValue.allowed_operations.includes(operation);
}
function modeReview(operation: string): boolean {
  const policy = props.modelValue;
  const planReview = typeof policy.plan_review === "boolean" ? policy.plan_review : ["direction", "edit"].includes(policy.mode);
  const stepReview = typeof policy.key_step_review === "boolean" ? policy.key_step_review : policy.mode === "edit";
  return planReview && operation === "draft_quest"
    || stepReview && ["draft_logic", "logic_contract", "draft_dialogue", "dialogue_constrained"].includes(operation);
}
function requiresReview(operation: string): boolean {
  return modeReview(operation) || (props.modelValue.review_operations ?? []).includes(operation);
}
function toggleAllowed(operation: string, event: Event): void {
  if (props.disabled || !registered.has(operation)) return;
  const selected = new Set(props.modelValue.allowed_operations ?? operationIds);
  if ((event.target as HTMLInputElement).checked) selected.add(operation); else selected.delete(operation);
  emit("update:modelValue", { ...props.modelValue, allowed_operations: [...selected].sort() });
}
function toggleReview(operation: string, event: Event): void {
  if (props.disabled || !registered.has(operation) || !allowed(operation) || modeReview(operation)) return;
  const selected = new Set(props.modelValue.review_operations ?? []);
  if ((event.target as HTMLInputElement).checked) selected.add(operation); else selected.delete(operation);
  emit("update:modelValue", { ...props.modelValue, review_operations: [...selected].sort() });
}
</script>

<template>
  <details class="stage-settings">
    <summary>制作步骤设置</summary>
    <p :id="`${id}-scope`" class="hint">只约束下面列出的制作步骤；已有参与方式的确认点继续保留</p>
    <div v-if="visibleOperations.length" class="operation-list" :aria-describedby="`${id}-scope`">
      <fieldset v-for="item in visibleOperations" :key="item.operation" :disabled="disabled" class="operation-row">
        <legend>{{ item.label }}</legend>
        <label><input type="checkbox" :checked="allowed(item.operation)" @change="toggleAllowed(item.operation, $event)">允许执行</label>
        <label><input type="checkbox" :checked="requiresReview(item.operation)" :disabled="!allowed(item.operation) || modeReview(item.operation)" @change="toggleReview(item.operation, $event)">{{ modeReview(item.operation) ? '按参与方式确认' : '额外确认' }}</label>
      </fieldset>
    </div>
    <p v-else class="hint">当前没有可配置的制作步骤。</p>
  </details>
</template>

<style scoped>
.stage-settings { border-top: 1px solid var(--ow-paper-edge); padding-top: .7rem; }
summary { cursor: pointer; color: var(--ow-ink-soft); font-size: .79rem; }.hint { color: var(--ow-ink-mute); font-size: .73rem; line-height: 1.8; margin: .6rem 0; }
.operation-list { display: grid; gap: .4rem; }.operation-row { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; margin: 0; padding: .55rem .7rem .65rem; border: 1px solid var(--ow-paper-edge); border-radius: 5px; min-width: 0; }.operation-row legend { font-size: .76rem; color: var(--ow-ink-soft); padding: 0 .25rem; }.operation-row label { display: flex; align-items: center; gap: .45rem; color: var(--ow-ink-soft); font-size: .74rem; line-height: 1.6; }.operation-row input { accent-color: var(--ow-gold); flex: 0 0 auto; }.operation-row:disabled { opacity: .5; }
@media (max-width: 500px) { .operation-row { grid-template-columns: 1fr; } }
</style>
