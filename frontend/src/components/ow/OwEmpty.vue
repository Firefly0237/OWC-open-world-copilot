<script setup lang="ts">
withDefaults(
  defineProps<{ title: string; hint?: string; cta?: string; compact?: boolean }>(),
  { hint: "", cta: "", compact: false },
);
const emit = defineEmits<{ action: [] }>();
</script>

<template>
  <div class="ow-empty" :class="{ compact }">
    <svg class="ow-empty-sigil" viewBox="0 0 120 90" width="120" height="90" aria-hidden="true">
      <g fill="none" stroke="var(--ow2-gold)" stroke-width="1" opacity="0.55">
        <path d="M18 62 L44 38 L72 50 L102 22" />
        <circle cx="18" cy="62" r="3" />
        <circle cx="44" cy="38" r="3" />
        <circle cx="72" cy="50" r="3" />
        <circle cx="102" cy="22" r="3" />
      </g>
      <g fill="var(--ow2-gold)">
        <circle cx="18" cy="62" r="2" />
        <circle cx="44" cy="38" r="2" />
        <circle cx="72" cy="50" r="2" />
        <circle cx="102" cy="22" r="2.4" />
      </g>
      <path d="M6 78 q26 -10 52 0 t52 0" fill="none" stroke="var(--ow2-gold)" stroke-width="0.8" opacity="0.35" />
    </svg>
    <p class="ow-empty-title">{{ title }}</p>
    <p v-if="hint" class="ow-empty-hint">{{ hint }}</p>
    <button v-if="cta" type="button" class="ow-empty-cta" @click="emit('action')">{{ cta }}</button>
    <slot />
  </div>
</template>

<style scoped>
.ow-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 44px 24px;
  gap: 8px;
}
.ow-empty.compact {
  padding: 24px 16px;
}
.ow-empty-sigil {
  opacity: 0.9;
}
.ow-empty-title {
  font-family: var(--ow2-serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--ow2-ink);
  margin: 6px 0 0;
}
.ow-empty-hint {
  font-size: 12px;
  color: var(--ow2-ink-soft);
  max-width: 420px;
  line-height: 1.7;
  margin: 0;
}
.ow-empty-cta {
  margin-top: 10px;
  font-family: var(--ow2-sans);
  font-size: 12.5px;
  font-weight: 600;
  padding: 8px 20px;
  border-radius: var(--ow2-radius-control);
  border: none;
  cursor: pointer;
  color: #4a3a1c;
  background: linear-gradient(180deg, #d3b578, #b8965a);
  box-shadow: 0 2px 6px rgba(158, 127, 66, 0.35);
  transition: all var(--ow2-dur);
}
.ow-empty-cta:hover {
  filter: brightness(1.05);
}
</style>
