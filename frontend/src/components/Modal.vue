<script setup lang="ts">
// HSR cut-corner dialog: gilded corner brackets, scrim, Esc / scrim-click / close-button to dismiss,
// body scrolls when tall. Header takes an English overline + CJK title. Reusable for drill-down
// lists, detail popups, confirmations — anywhere a panel should float over the workbench.
import { onMounted, onUnmounted } from "vue";

const props = defineProps<{
  open: boolean;
  title: string;
  overline?: string;
  count?: number;
  wide?: boolean;
}>();
const emit = defineEmits<{ close: [] }>();

function onKey(e: KeyboardEvent): void {
  if (props.open && e.key === "Escape") emit("close");
}
onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <Transition name="modal">
    <div v-if="open" class="modal-root" @click.self="emit('close')">
      <div class="modal pane violet" :class="{ wide }" role="dialog" aria-modal="true">
        <header class="modal-head">
          <div class="mh-titles">
            <span v-if="overline" class="overline">{{ overline }}</span>
            <h2 class="mh-title">
              {{ title }}<span v-if="count != null" class="mh-count num">{{ count }}</span>
            </h2>
          </div>
          <button class="modal-x" aria-label="关闭" @click="emit('close')">
            <span></span><span></span>
          </button>
        </header>
        <div class="modal-body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="modal-foot"><slot name="footer" /></footer>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.modal-root {
  position: fixed;
  inset: 0;
  z-index: 8000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.4rem;
  background: rgba(6, 9, 22, 0.66);
  backdrop-filter: blur(3px);
}
.modal {
  width: min(680px, 100%);
  max-height: min(82vh, 760px);
  display: flex;
  flex-direction: column;
  padding: 0;
  /* the shared .pane animation is a fade-up; the modal adds its own scale-in via the transition */
  animation: none;
}
.modal.wide {
  width: min(940px, 100%);
}
.modal-head {
  display: flex;
  align-items: flex-end;
  gap: 1rem;
  padding: 1.05rem 1.2rem 0.7rem 1.3rem;
  border-bottom: 1px solid var(--ow-edge-violet);
}
.mh-titles {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
  min-width: 0;
}
.mh-title {
  margin: 0;
  font-size: 1.18rem;
  display: flex;
  align-items: baseline;
  gap: 0.55rem;
}
.mh-count {
  color: var(--ow-gold-bright);
  font-size: 0.92rem;
  font-variant-numeric: tabular-nums;
}
.modal-x {
  margin-left: auto;
  flex: none;
  position: relative;
  width: 30px;
  height: 30px;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  background: transparent;
  cursor: pointer;
}
.modal-x span {
  position: absolute;
  left: 7px;
  top: 14px;
  width: 14px;
  height: 1.5px;
  background: var(--ow-muted);
  transition: background 0.15s ease;
}
.modal-x span:first-child {
  transform: rotate(45deg);
}
.modal-x span:last-child {
  transform: rotate(-45deg);
}
.modal-x:hover {
  border-color: var(--ow-gold-soft);
}
.modal-x:hover span {
  background: var(--ow-gold-bright);
}
.modal-body {
  overflow-y: auto;
  padding: 0.9rem 1.2rem 1.1rem 1.3rem;
}
.modal-foot {
  border-top: 1px solid var(--ow-line);
  padding: 0.75rem 1.2rem;
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.22s ease;
}
.modal-enter-active .modal,
.modal-leave-active .modal {
  transition: transform 0.24s cubic-bezier(0.2, 0.9, 0.3, 1.1), opacity 0.22s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal,
.modal-leave-to .modal {
  transform: translateY(10px) scale(0.97);
  opacity: 0;
}
@media (prefers-reduced-motion: reduce) {
  .modal-enter-active .modal,
  .modal-leave-active .modal {
    transition: none;
  }
}
</style>
