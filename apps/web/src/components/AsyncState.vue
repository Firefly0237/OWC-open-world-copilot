<script setup lang="ts">
withDefaults(
  defineProps<{
    state: "loading" | "empty" | "error";
    title: string;
    hint?: string;
    actionLabel?: string;
    compact?: boolean;
  }>(),
  { hint: "", actionLabel: "", compact: false },
);

const emit = defineEmits<{ action: [] }>();
</script>

<template>
  <div
    class="async-state"
    :class="[`is-${state}`, { compact }]"
    :role="state === 'error' ? 'alert' : 'status'"
    :aria-live="state === 'error' ? 'assertive' : 'polite'"
    :aria-busy="state === 'loading' || undefined"
  >
    <div class="state-sigil" aria-hidden="true">
      <span class="state-orbit"></span>
      <span class="state-core"></span>
      <span v-if="state === 'error'" class="state-alert">!</span>
    </div>
    <div class="state-copy">
      <p class="state-title">{{ title }}</p>
      <p v-if="hint" class="state-hint">{{ hint }}</p>
    </div>
    <button v-if="actionLabel" type="button" class="state-action" @click="emit('action')">
      {{ actionLabel }}
    </button>
    <div v-if="$slots.default" class="state-extra"><slot /></div>
  </div>
</template>

<style scoped>
.async-state {
  --state-color: var(--ow-gold-bright);
  display: flex;
  min-height: 220px;
  padding: clamp(1.5rem, 4vw, 2.6rem);
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.7rem;
  text-align: center;
  color: var(--ow-ink);
  background:
    linear-gradient(180deg, rgba(240, 210, 138, 0.045), transparent 34%),
    var(--ow-panel);
  border: 1px solid var(--ow-edge);
  border-radius: var(--ow-control-radius);
  clip-path: polygon(
    var(--ow-cut) 0,
    100% 0,
    100% calc(100% - var(--ow-cut)),
    calc(100% - var(--ow-cut)) 100%,
    0 100%,
    0 var(--ow-cut)
  );
  box-shadow: inset 0 1px 0 rgba(240, 210, 138, 0.1);
}

.async-state.compact {
  min-height: 132px;
  padding: 1.15rem;
}

.async-state.is-error {
  --state-color: #e89a9a;
  border-color: rgba(224, 133, 133, 0.48);
}

.async-state.is-empty {
  --state-color: var(--ow-cyan);
}

.state-sigil {
  position: relative;
  width: 46px;
  height: 46px;
}

.compact .state-sigil {
  width: 36px;
  height: 36px;
}

.state-orbit {
  position: absolute;
  inset: 0;
  border: 1px dashed color-mix(in srgb, var(--state-color) 58%, transparent);
  border-radius: 50%;
}

.is-loading .state-orbit {
  animation: state-spin 1.8s linear infinite;
}

.state-core {
  position: absolute;
  inset: 14px;
  background: var(--state-color);
  clip-path: polygon(50% 0, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0 50%, 40% 40%);
  box-shadow: 0 0 12px color-mix(in srgb, var(--state-color) 50%, transparent);
}

.compact .state-core {
  inset: 11px;
}

.is-error .state-core {
  inset: 9px;
  opacity: 0.18;
}

.state-alert {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--state-color);
  font-family: var(--ow-num);
  font-size: 1.1rem;
  font-style: normal;
  font-weight: 700;
}

.state-copy {
  max-width: 48ch;
}

.state-title,
.state-hint {
  margin: 0;
}

.state-title {
  font-family: var(--ow-display);
  font-size: 0.96rem;
  font-weight: 600;
  color: var(--ow-ink);
}

.state-hint {
  margin-top: 0.3rem;
  color: var(--ow-muted);
  font-size: 0.82rem;
  line-height: 1.6;
}

.state-action {
  min-height: 44px;
  padding: 0.48rem 1rem;
  border: 1px solid var(--ow-gold-soft);
  border-radius: var(--ow-control-radius);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
  font-weight: 600;
}

.state-action:hover {
  border-color: var(--ow-gold-bright);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.18);
}

.state-action:focus-visible {
  outline: 2px solid var(--ow-cyan);
  outline-offset: 3px;
}

.state-extra {
  margin-top: 0.2rem;
}

@keyframes state-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-loading .state-orbit {
    animation: none;
    border-style: solid;
  }
}
</style>
