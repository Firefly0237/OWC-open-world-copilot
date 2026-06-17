<script setup lang="ts">
// A composed "standing-by" panel for any output/list zone that has no data yet — so a page reads
// as a console awaiting input rather than dead space. Copy stays concise: say what appears here,
// nothing about workflow. Pass a slot for an optional primary action.
defineProps<{ title: string; hint?: string; busy?: boolean }>();
</script>

<template>
  <div class="empty pane" :class="{ busy }">
    <div class="sigil" aria-hidden="true">
      <span class="ring r1"></span>
      <span class="ring r2"></span>
      <span class="star"></span>
    </div>
    <p class="empty-title">{{ title }}</p>
    <p v-if="hint" class="empty-hint muted">{{ hint }}</p>
    <div v-if="$slots.default" class="empty-action"><slot /></div>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 0.55rem;
  min-height: clamp(300px, 46vh, 520px);
  height: 100%;
  padding: 2.6rem 1.5rem;
}
.sigil {
  position: relative;
  width: 72px;
  height: 72px;
  margin-bottom: 0.4rem;
}
.ring {
  position: absolute;
  border-radius: 50%;
  border: 1px solid var(--ow-violet-soft);
}
.ring.r1 {
  inset: 0;
  border-style: dashed;
  animation: es-spin 28s linear infinite;
}
.ring.r2 {
  inset: 14px;
  border-color: rgba(143, 214, 232, 0.22);
  animation: es-spin 18s linear infinite reverse;
}
.star {
  position: absolute;
  inset: 26px;
  background: var(--ow-gold-soft);
  clip-path: polygon(50% 0, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0 50%, 40% 40%);
  animation: es-pulse 3.2s ease-in-out infinite;
}
.busy .star {
  background: var(--ow-gold-bright);
  box-shadow: 0 0 14px rgba(240, 210, 138, 0.6);
  animation: es-spin 2.2s linear infinite;
}
.empty-title {
  margin: 0;
  font-family: var(--ow-display);
  font-weight: 500;
  font-size: 1.02rem;
  color: var(--ow-ink);
}
.empty-hint {
  margin: 0;
  font-size: 0.85rem;
  line-height: 1.7;
  max-width: 40ch;
}
.empty-action {
  margin-top: 0.7rem;
}
@keyframes es-spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes es-pulse {
  0%,
  100% {
    opacity: 0.55;
    transform: scale(0.94);
  }
  50% {
    opacity: 1;
    transform: scale(1.06);
  }
}
@media (prefers-reduced-motion: reduce) {
  .ring,
  .star {
    animation: none;
  }
}
</style>
