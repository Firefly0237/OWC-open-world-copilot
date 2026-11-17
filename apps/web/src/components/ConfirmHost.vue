<script setup lang="ts">
import Modal from "./Modal.vue";
import { activeConfirm, settleConfirm } from "../confirm";
</script>

<template>
  <Modal
    :open="Boolean(activeConfirm)"
    overline="CONFIRM"
    :title="activeConfirm?.title ?? ''"
    @close="settleConfirm(false)"
  >
    <p class="confirm-copy">{{ activeConfirm?.message }}</p>
    <template #footer>
      <button type="button" class="cancel" data-modal-autofocus @click="settleConfirm(false)">
        {{ activeConfirm?.cancelLabel }}
      </button>
      <button
        type="button"
        class="accept"
        :class="{ danger: activeConfirm?.danger }"
        @click="settleConfirm(true)"
      >
        {{ activeConfirm?.confirmLabel }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.confirm-copy { margin: 0; color: var(--ow-ink); line-height: 1.75; white-space: pre-line; }
button { min-width: 6rem; min-height: 44px; padding: 0.5rem 0.9rem; border: 1px solid var(--ow-line); background: var(--ow-panel-2); color: var(--ow-ink); cursor: pointer; font: inherit; }
.cancel { color: var(--ow-muted); }
.accept { border-color: var(--ow-gold-soft); color: var(--ow-gold-bright); background: var(--ow-gold-faint); }
.accept.danger { border-color: rgba(224, 112, 90, 0.58); color: #f0a18e; background: rgba(224, 112, 90, 0.1); }
button:focus-visible { outline: 2px solid var(--ow-cyan); outline-offset: 2px; }
</style>
