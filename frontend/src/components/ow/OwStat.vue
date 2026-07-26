<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    label: string;
    value: string | number;
    hint?: string;
    hintTone?: "teal" | "amber" | "cinnabar" | "muted";
    to?: string;
  }>(),
  { hint: "", hintTone: "teal", to: "" },
);
const clickable = computed(() => Boolean(props.to));
</script>

<template>
  <component
    :is="clickable ? 'router-link' : 'div'"
    :to="to || undefined"
    class="ow-stat"
    :class="{ clickable }"
  >
    <span class="ow-stat-label"><i aria-hidden="true" />{{ label }}</span>
    <b class="ow-stat-value">{{ value }}</b>
    <em v-if="hint" class="ow-stat-hint" :class="`is-${hintTone}`">{{ hint }}</em>
  </component>
</template>

<style scoped>
.ow-stat {
  display: block;
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-pane);
  box-shadow: var(--ow2-shadow);
  padding: 12px 14px;
  text-decoration: none;
  transition: transform var(--ow2-dur), box-shadow var(--ow2-dur), border-color var(--ow2-dur);
}
.ow-stat.clickable:hover {
  transform: translateY(-2px);
  box-shadow: var(--ow2-shadow-lift);
  border-color: var(--ow2-gold);
}
.ow-stat-label {
  font-size: 11px;
  color: var(--ow2-ink-soft);
  display: flex;
  align-items: center;
  gap: 6px;
}
.ow-stat-label i {
  width: 5px;
  height: 5px;
  transform: rotate(45deg);
  background: var(--ow2-gold);
}
.ow-stat-value {
  font-size: 24px;
  font-weight: 400;
  font-family: var(--ow2-num);
  color: var(--ow2-ink);
  display: block;
  margin-top: 2px;
  line-height: 1.15;
}
.ow-stat-hint {
  font-style: normal;
  font-size: 10px;
}
.ow-stat-hint.is-teal { color: var(--ow2-teal); }
.ow-stat-hint.is-amber { color: var(--ow2-amber); }
.ow-stat-hint.is-cinnabar { color: var(--ow2-cinnabar); }
.ow-stat-hint.is-muted { color: var(--ow2-muted); }
</style>
