<script setup lang="ts">
export interface OwTabItem {
  key: string;
  label: string;
  badge?: number;
}

defineProps<{ items: OwTabItem[]; modelValue: string }>();
const emit = defineEmits<{ "update:modelValue": [key: string] }>();
</script>

<template>
  <div class="ow-tabs" role="tablist">
    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      class="ow-tab"
      :class="{ on: item.key === modelValue }"
      role="tab"
      :aria-selected="item.key === modelValue"
      @click="emit('update:modelValue', item.key)"
    >
      <i class="ow-tab-diamond" aria-hidden="true" />
      {{ item.label }}
      <span v-if="item.badge" class="ow-tab-badge">{{ item.badge }}</span>
    </button>
  </div>
</template>

<style scoped>
.ow-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--ow2-gold-soft);
  overflow-x: auto;
}
.ow-tab {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-family: var(--ow2-sans);
  font-size: 12.5px;
  color: var(--ow2-ink-soft);
  padding: 8px 16px 9px;
  cursor: pointer;
  border: none;
  background: none;
  position: relative;
  white-space: nowrap;
}
.ow-tab-diamond {
  width: 6px;
  height: 6px;
  transform: rotate(45deg);
  border: 1px solid var(--ow2-muted);
  transition: all var(--ow2-dur);
}
.ow-tab:hover {
  color: var(--ow2-ink);
}
.ow-tab:focus-visible {
  outline: none;
  box-shadow: inset 0 0 0 2px var(--ow2-gold-soft);
  border-radius: 4px;
}
.ow-tab.on {
  color: var(--ow2-gold-deep);
  font-weight: 600;
}
.ow-tab.on .ow-tab-diamond {
  background: var(--ow2-gold);
  border-color: var(--ow2-gold);
}
.ow-tab.on::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: -1px;
  height: 2px;
  background: var(--ow2-gold);
  border-radius: 1px;
}
.ow-tab-badge {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 6px;
  color: var(--ow2-cinnabar);
  border: 1px solid rgba(207, 91, 75, 0.4);
}
</style>
