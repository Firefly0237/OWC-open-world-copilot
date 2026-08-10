<script setup lang="ts">
import OwButton from "./OwButton.vue";

withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    danger?: boolean;
    confirmText?: string;
    cancelText?: string;
    persistent?: boolean;
  }>(),
  { danger: false, confirmText: "确认", cancelText: "取消", persistent: false },
);
const emit = defineEmits<{ confirm: []; cancel: [] }>();
function onCancel(): void {
  emit("cancel");
}
function onScrim(): void {
  emit("cancel");
}
</script>

<template>
  <Teleport to="body">
    <Transition name="ow-modal">
      <div v-if="open" class="ow-modal-scrim" @click.self="persistent ? undefined : onScrim()">
        <div class="ow-modal ow2" role="dialog" aria-modal="true" :aria-label="title">
          <header class="ow-modal-head">
            <span class="ow2-overline">CONFIRM</span>
            <h3 class="ow-modal-title">{{ title }}</h3>
          </header>
          <div class="ow-modal-body"><slot /></div>
          <footer class="ow-modal-foot">
            <slot name="footer">
              <OwButton variant="ghost" @click="onCancel">{{ cancelText }}</OwButton>
              <OwButton :variant="danger ? 'danger' : 'primary'" @click="emit('confirm')">{{ confirmText }}</OwButton>
            </slot>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ow-modal-scrim {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: var(--ow2-scrim, rgba(62, 58, 48, 0.35));
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.ow-modal {
  width: min(480px, 100%);
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-pane);
  box-shadow: var(--ow2-shadow-lift);
  padding: 20px 22px;
}
.ow-modal-title {
  font-family: var(--ow2-serif);
  font-size: 17px;
  color: var(--ow2-ink);
  margin: 4px 0 0;
}
.ow-modal-body {
  font-size: 12.5px;
  color: var(--ow2-ink-soft);
  line-height: 1.7;
  margin: 12px 0 18px;
}
.ow-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.ow-modal-enter-active,
.ow-modal-leave-active {
  transition: opacity 0.18s ease;
}
.ow-modal-enter-from,
.ow-modal-leave-to {
  opacity: 0;
}
.ow-modal-enter-active .ow-modal {
  animation: ow-modal-in 0.22s var(--ow2-ease-spring);
}
@keyframes ow-modal-in {
  from { transform: translateY(10px) scale(0.98); }
  to { transform: none; }
}
</style>
