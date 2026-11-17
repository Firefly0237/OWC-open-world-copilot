<script setup lang="ts">
// HSR cut-corner dialog: gilded corner brackets, scrim, Esc / scrim-click / close-button to dismiss,
// body scrolls when tall. Header takes an English overline + CJK title. Reusable for drill-down
// lists, detail popups, confirmations — anywhere a panel should float over the workbench.
import { nextTick, onMounted, onUnmounted, ref, useId, watch } from "vue";

const props = defineProps<{
  open: boolean;
  title: string;
  overline?: string;
  count?: number;
  wide?: boolean;
}>();
const emit = defineEmits<{ close: [] }>();
const root = ref<HTMLElement | null>(null);
const dialog = ref<HTMLElement | null>(null);
const titleId = `dialog-title-${useId()}`;
let returnFocus: HTMLElement | null = null;
let bodyLockHeld = false;

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

function close(): void { emit("close"); }

function acquireBodyLock(): void {
  if (bodyLockHeld) return;
  bodyLockHeld = true;
  const count = Number.parseInt(document.body.dataset.owModalCount ?? "0", 10) || 0;
  document.body.dataset.owModalCount = String(count + 1);
  document.body.classList.add("modal-open");
}

function releaseBodyLock(): void {
  if (!bodyLockHeld) return;
  bodyLockHeld = false;
  const count = Math.max(0, (Number.parseInt(document.body.dataset.owModalCount ?? "1", 10) || 1) - 1);
  if (count > 0) {
    document.body.dataset.owModalCount = String(count);
    document.body.classList.add("modal-open");
  } else {
    delete document.body.dataset.owModalCount;
    document.body.classList.remove("modal-open");
  }
}

function isTopModal(): boolean {
  if (!root.value) return false;
  const roots = document.querySelectorAll<HTMLElement>(".modal-root");
  return roots.length > 0 && roots[roots.length - 1] === root.value;
}

function onKey(e: KeyboardEvent): void {
  if (!props.open || !isTopModal()) return;
  if (e.key === "Escape") {
    e.preventDefault();
    close();
    return;
  }
  if (e.key !== "Tab" || !dialog.value) return;
  const focusable = [...dialog.value.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter((item) => item.offsetParent !== null);
  if (!focusable.length) {
    e.preventDefault();
    dialog.value.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (active === dialog.value || !dialog.value.contains(active)) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  } else if (e.shiftKey && active === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}
watch(
  () => props.open,
  async (open) => {
    if (open) {
      returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      acquireBodyLock();
      await nextTick();
      if (!props.open) return;
      const initial = dialog.value?.querySelector<HTMLElement>("[data-modal-autofocus]");
      (initial ?? dialog.value)?.focus();
    } else {
      releaseBodyLock();
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      returnFocus = null;
    }
  },
  { immediate: true },
);
onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => {
  releaseBodyLock();
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <Transition name="modal">
    <div v-if="open" ref="root" class="modal-root" @click.self="close">
      <div
        ref="dialog"
        class="modal pane violet"
        :class="{ wide }"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
      >
        <header class="modal-head">
          <div class="mh-titles">
            <span v-if="overline" class="overline">{{ overline }}</span>
            <h2 :id="titleId" class="mh-title">
              {{ title }}<span v-if="count != null" class="mh-count num">{{ count }}</span>
            </h2>
          </div>
          <button type="button" class="modal-x" aria-label="关闭" @click="close">
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
:global(body.modal-open) { overflow: hidden; }
.modal:focus { outline: none; }
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
  width: 44px;
  height: 44px;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: transparent;
  cursor: pointer;
}
.modal-x span {
  position: absolute;
  left: 12px;
  top: 21px;
  width: 18px;
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
