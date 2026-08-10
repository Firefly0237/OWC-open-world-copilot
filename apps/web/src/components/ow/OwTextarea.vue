<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = withDefaults(
  defineProps<{
    modelValue: string;
    placeholder?: string;
    maxlength?: number;
    rows?: number;
    autosize?: boolean;
    disabled?: boolean;
  }>(),
  { placeholder: "", maxlength: undefined, rows: 4, autosize: false, disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const el = ref<HTMLTextAreaElement | null>(null);

const count = computed(() => props.modelValue.length);
const ratio = computed(() => (props.maxlength ? Math.min(1, count.value / props.maxlength) : 0));
const nearLimit = computed(() => ratio.value >= 0.9);
const R = 8;
const CIRC = 2 * Math.PI * R;

function fitHeight(): void {
  if (!props.autosize || !el.value) return;
  el.value.style.height = "auto";
  el.value.style.height = `${el.value.scrollHeight + 2}px`;
}
function onInput(event: Event): void {
  emit("update:modelValue", (event.target as HTMLTextAreaElement).value);
  fitHeight();
}
watch(() => props.modelValue, fitHeight);
</script>

<template>
  <div class="ow-ta" :class="{ 'near-limit': nearLimit }">
    <textarea
      ref="el"
      class="ow-ta-el"
      :value="modelValue"
      :placeholder="placeholder"
      :maxlength="maxlength"
      :rows="autosize ? 1 : rows"
      :disabled="disabled"
      @input="onInput"
    />
    <span v-if="maxlength" class="ow-ta-count" :title="`${count}/${maxlength}`">
      <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
        <circle cx="10" cy="10" :r="R" fill="none" stroke="var(--ow2-line)" stroke-width="2" />
        <circle
          cx="10"
          cy="10"
          :r="R"
          fill="none"
          :stroke="nearLimit ? 'var(--ow2-cinnabar)' : 'var(--ow2-gold)'"
          stroke-width="2"
          stroke-linecap="round"
          :stroke-dasharray="CIRC"
          :stroke-dashoffset="CIRC * (1 - ratio)"
          transform="rotate(-90 10 10)"
        />
      </svg>
      <i>{{ count }}/{{ maxlength }}</i>
    </span>
  </div>
</template>

<style scoped>
.ow-ta {
  position: relative;
  width: 100%;
}
.ow-ta-el {
  width: 100%;
  background: var(--ow2-field);
  border: 1px solid var(--ow2-line);
  border-radius: var(--ow2-radius-control);
  padding: 9px 12px;
  font-size: 12.5px;
  font-family: var(--ow2-sans);
  color: var(--ow2-ink);
  outline: none;
  resize: vertical;
  line-height: 1.6;
  transition: border-color var(--ow2-dur), box-shadow var(--ow2-dur), background var(--ow2-dur);
}
.ow-ta-el::placeholder {
  color: var(--ow2-muted);
}
.ow-ta-el:hover:not(:disabled) {
  border-color: var(--ow2-gold-soft);
}
.ow-ta-el:focus {
  border-color: var(--ow2-gold);
  box-shadow: 0 0 0 3px var(--ow2-gold-faint);
  background: var(--ow2-card);
}
.ow-ta-el:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.ow-ta-count {
  position: absolute;
  right: 10px;
  bottom: 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  pointer-events: none;
}
.ow-ta-count i {
  font-style: normal;
  font-size: 10px;
  color: var(--ow2-muted);
  font-family: var(--ow2-num);
}
.near-limit .ow-ta-count i {
  color: var(--ow2-cinnabar);
}
</style>
