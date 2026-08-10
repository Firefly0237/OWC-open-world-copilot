<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: "primary" | "secondary" | "ghost" | "danger";
    loading?: boolean;
    disabled?: boolean;
    type?: "button" | "submit";
  }>(),
  { variant: "secondary", loading: false, disabled: false, type: "button" },
);
const emit = defineEmits<{ click: [event: MouseEvent] }>();
function onClick(event: MouseEvent): void {
  emit("click", event);
}
</script>

<template>
  <button
    class="ow-btn"
    :class="[`is-${variant}`, { 'is-loading': loading }]"
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading || undefined"
    @click="onClick"
  >
    <span v-if="loading" class="ow-btn-orbit" aria-hidden="true"><i /></span>
    <span class="ow-btn-label"><slot /></span>
  </button>
</template>

<style scoped>
.ow-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--ow2-sans);
  font-size: 12.5px;
  font-weight: 500;
  padding: 8px 18px;
  border-radius: var(--ow2-radius-control);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all var(--ow2-dur);
  white-space: nowrap;
}
.ow-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ow2-gold-soft);
}
.ow-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.is-primary {
  background: linear-gradient(180deg, #d3b578, #b8965a);
  color: #4a3a1c;
  font-weight: 600;
  box-shadow: 0 2px 6px rgba(158, 127, 66, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.35);
}
.is-primary:hover:not(:disabled) {
  filter: brightness(1.05);
  box-shadow: 0 3px 10px rgba(158, 127, 66, 0.45);
}
.is-secondary {
  background: transparent;
  border-color: var(--ow2-gold);
  color: var(--ow2-gold-deep);
}
.is-secondary:hover:not(:disabled) {
  background: var(--ow2-gold-faint);
}
.is-ghost {
  background: transparent;
  color: var(--ow2-ink-soft);
  border-color: var(--ow2-line);
}
.is-ghost:hover:not(:disabled) {
  color: var(--ow2-ink);
  border-color: var(--ow2-gold-soft);
}
.is-danger {
  background: transparent;
  border-color: var(--ow2-cinnabar);
  color: var(--ow2-cinnabar);
}
.is-danger:hover:not(:disabled) {
  background: rgba(207, 91, 75, 0.08);
}
.ow-btn-orbit {
  width: 14px;
  height: 14px;
  border: 1.5px solid currentColor;
  border-radius: 50%;
  position: relative;
  opacity: 0.85;
}
.ow-btn-orbit i {
  position: absolute;
  top: -2.5px;
  left: 50%;
  width: 4px;
  height: 4px;
  margin-left: -2px;
  border-radius: 50%;
  background: currentColor;
  transform-origin: 2px 9.5px;
  animation: ow-btn-spin 0.9s linear infinite;
}
@keyframes ow-btn-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
