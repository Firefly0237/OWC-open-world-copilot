<script setup lang="ts">
import type { OwTone } from "./OwBadge.vue";

withDefaults(defineProps<{ tone?: OwTone; removable?: boolean }>(), {
  tone: "neutral",
  removable: false,
});
const emit = defineEmits<{ remove: [] }>();
</script>

<template>
  <span class="ow-tag" :class="`is-${tone}`">
    <slot />
    <button v-if="removable" type="button" class="ow-tag-x" aria-label="移除" @click="emit('remove')">×</button>
  </span>
</template>

<style scoped>
.ow-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-family: var(--ow2-sans);
  padding: 3px 9px;
  border-radius: var(--ow2-radius-chip);
  border: 1px solid;
}
.is-gold { color: var(--ow2-gold-deep); border-color: var(--ow2-gold-soft); background: var(--ow2-gold-faint); }
.is-violet { color: var(--ow2-violet); border-color: rgba(155, 134, 214, 0.45); background: rgba(155, 134, 214, 0.08); }
.is-green { color: var(--ow2-green); border-color: rgba(134, 169, 78, 0.45); background: rgba(134, 169, 78, 0.08); }
.is-amber { color: var(--ow2-amber); border-color: rgba(210, 154, 60, 0.45); background: rgba(210, 154, 60, 0.08); }
.is-cinnabar { color: var(--ow2-cinnabar); border-color: rgba(207, 91, 75, 0.4); background: rgba(207, 91, 75, 0.07); }
.is-sky { color: var(--ow2-sky); border-color: rgba(127, 169, 207, 0.45); background: rgba(127, 169, 207, 0.09); }
.is-neutral { color: var(--ow2-ink-soft); border-color: var(--ow2-line); background: var(--ow2-field); }
.ow-tag-x {
  border: none;
  background: none;
  color: inherit;
  font-size: 13px;
  line-height: 1;
  padding: 0;
  cursor: pointer;
  opacity: 0.65;
}
.ow-tag-x:hover {
  opacity: 1;
}
</style>
