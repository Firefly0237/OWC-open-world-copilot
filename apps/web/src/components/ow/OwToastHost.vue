<script setup lang="ts">
import { owToasts } from "./owToast";
</script>

<template>
  <div class="ow-toast-host" aria-live="polite">
    <TransitionGroup name="ow-toast">
      <div v-for="item in owToasts" :key="item.id" class="ow-toast" :class="`is-${item.tone}`">
        <i class="ow-toast-diamond" aria-hidden="true" />
        {{ item.text }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.ow-toast-host {
  position: fixed;
  top: 18px;
  right: 18px;
  z-index: 120;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}
.ow-toast {
  display: flex;
  align-items: center;
  gap: 9px;
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-control);
  box-shadow: var(--ow2-shadow-lift);
  font-size: 12.5px;
  color: var(--ow2-ink);
  padding: 10px 14px;
  max-width: 340px;
}
.ow-toast-diamond {
  width: 6px;
  height: 6px;
  transform: rotate(45deg);
  background: var(--ow2-gold);
  flex: none;
}
.ow-toast.is-success .ow-toast-diamond { background: var(--ow2-green); }
.ow-toast.is-error .ow-toast-diamond { background: var(--ow2-cinnabar); }
.ow-toast.is-error { border-color: rgba(207, 91, 75, 0.4); }
.ow-toast-enter-active,
.ow-toast-leave-active {
  transition: all 0.22s ease;
}
.ow-toast-enter-from {
  opacity: 0;
  transform: translateX(16px);
}
.ow-toast-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
