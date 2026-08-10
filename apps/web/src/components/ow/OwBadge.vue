<script setup lang="ts">
export type OwTone = "gold" | "violet" | "green" | "amber" | "cinnabar" | "sky" | "neutral";
withDefaults(defineProps<{ tone?: OwTone }>(), { tone: "gold" });
</script>

<template>
  <span class="ow-badge" :class="`is-${tone}`"><slot /></span>
</template>

<style scoped>
.ow-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-family: var(--ow2-sans);
  padding: 2px 8px;
  border-radius: var(--ow2-radius-chip);
  border: 1px solid;
  white-space: nowrap;
}
.is-gold { color: var(--ow2-gold-deep); border-color: var(--ow2-gold-soft); background: var(--ow2-gold-faint); }
.is-violet { color: var(--ow2-violet); border-color: rgba(155, 134, 214, 0.45); background: rgba(155, 134, 214, 0.08); }
.is-green { color: var(--ow2-green); border-color: rgba(134, 169, 78, 0.45); background: rgba(134, 169, 78, 0.08); }
.is-amber { color: var(--ow2-amber); border-color: rgba(210, 154, 60, 0.45); background: rgba(210, 154, 60, 0.08); }
.is-cinnabar { color: var(--ow2-cinnabar); border-color: rgba(207, 91, 75, 0.4); background: rgba(207, 91, 75, 0.07); }
.is-sky { color: var(--ow2-sky); border-color: rgba(127, 169, 207, 0.45); background: rgba(127, 169, 207, 0.09); }
.is-neutral { color: var(--ow2-ink-soft); border-color: var(--ow2-line); background: transparent; }
</style>
