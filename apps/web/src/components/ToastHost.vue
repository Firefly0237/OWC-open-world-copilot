<script setup lang="ts">
// Renders the global toast stack, top-right, above everything. Each toast is an HSR cut-corner
// sliver with a colored accent edge; click or wait to dismiss.
import { toasts, dismissToast } from "../toast";
</script>

<template>
  <div class="toast-host" aria-live="polite">
    <TransitionGroup name="toast">
      <div
        v-for="t in toasts"
        :key="t.id"
        class="toast pane"
        :class="t.kind"
        :role="t.kind === 'error' ? 'alert' : 'status'"
      >
        <span class="toast-mark" aria-hidden="true"></span>
        <span class="toast-msg">{{ t.message }}</span>
        <button type="button" class="toast-close" :aria-label="`关闭提示：${t.message}`" @click="dismissToast(t.id)">×</button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  top: 1rem;
  right: 1rem;
  z-index: 9500;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  width: min(360px, calc(100vw - 2rem));
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  padding: 0.7rem 0.9rem;
  font-size: 0.85rem;
  line-height: 1.55;
  color: var(--ow-ink);
  animation: none;
}
.toast-close {
  flex: none;
  width: 32px;
  height: 32px;
  margin: -0.2rem -0.35rem -0.2rem auto;
  padding: 0;
  border: 0;
  color: var(--ow-muted);
  background: transparent;
  cursor: pointer;
  font-size: 1.1rem;
}
.toast-close:hover { color: var(--ow-gold-bright); }
.toast-mark {
  flex: none;
  width: 7px;
  height: 7px;
  margin-top: 0.42rem;
  border-radius: 50%;
  clip-path: polygon(50% 0, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0 50%, 40% 40%);
}
.toast.error {
  border-color: rgba(224, 133, 133, 0.5);
}
.toast.error .toast-mark {
  background: #e08585;
  box-shadow: 0 0 8px rgba(224, 133, 133, 0.7);
}
.toast.ok {
  border-color: rgba(142, 212, 172, 0.5);
}
.toast.ok .toast-mark {
  background: #8ed4ac;
  box-shadow: 0 0 8px rgba(142, 212, 172, 0.7);
}
.toast.info .toast-mark {
  background: var(--ow-gold-bright);
  box-shadow: 0 0 8px rgba(240, 210, 138, 0.7);
}
.toast-msg {
  min-width: 0;
}

.toast-enter-active,
.toast-leave-active {
  transition: transform 0.28s cubic-bezier(0.2, 0.9, 0.3, 1.1), opacity 0.24s ease;
}
.toast-enter-from {
  transform: translateX(20px);
  opacity: 0;
}
.toast-leave-to {
  transform: translateX(20px);
  opacity: 0;
}
.toast-leave-active {
  position: absolute;
  right: 0;
}
@media (prefers-reduced-motion: reduce) {
  .toast-enter-active,
  .toast-leave-active {
    transition: opacity 0.2s ease;
  }
  .toast-enter-from,
  .toast-leave-to {
    transform: none;
  }
}
</style>
