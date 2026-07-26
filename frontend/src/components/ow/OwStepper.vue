<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{ steps: string[]; current: number }>(), { current: 0 });
const normalized = computed(() => Math.max(0, Math.min(props.current, props.steps.length - 1)));
</script>

<template>
  <ol class="ow-steps">
    <li
      v-for="(step, index) in steps"
      :key="step"
      class="ow-step"
      :class="{ done: index < normalized, current: index === normalized }"
    >
      <span class="ow-step-dot" aria-hidden="true">
        <svg v-if="index < normalized" viewBox="0 0 10 10" width="8" height="8"><path d="M1.5 5.5 L4 8 L8.5 2.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
      </span>
      <span class="ow-step-label">{{ step }}</span>
      <i v-if="index < steps.length - 1" class="ow-step-line" aria-hidden="true" />
    </li>
  </ol>
</template>

<style scoped>
.ow-steps {
  display: flex;
  align-items: flex-start;
  list-style: none;
  margin: 0;
  padding: 0;
}
.ow-step {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.ow-step:last-child {
  flex: none;
}
.ow-step-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 1.5px solid var(--ow2-line);
  background: var(--ow2-card);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fdfbf5;
  flex: none;
  transition: all var(--ow2-dur);
}
.ow-step.done .ow-step-dot {
  background: var(--ow2-gold);
  border-color: var(--ow2-gold);
}
.ow-step.current .ow-step-dot {
  border-color: var(--ow2-gold);
  box-shadow: 0 0 0 3px var(--ow2-gold-faint);
}
.ow-step-label {
  font-size: 12px;
  color: var(--ow2-muted);
  white-space: nowrap;
}
.ow-step.current .ow-step-label {
  color: var(--ow2-gold-deep);
  font-weight: 600;
}
.ow-step.done .ow-step-label {
  color: var(--ow2-ink-soft);
}
.ow-step-line {
  flex: 1;
  height: 1px;
  background: var(--ow2-line);
  margin: 0 10px;
  min-width: 16px;
}
.ow-step.done .ow-step-line {
  background: var(--ow2-gold-soft);
}
</style>
