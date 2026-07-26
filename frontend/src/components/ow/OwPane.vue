<script setup lang="ts">
withDefaults(defineProps<{ ornament?: boolean; pad?: boolean }>(), {
  ornament: false,
  pad: true,
});
</script>

<template>
  <section class="ow-pane" :class="{ ornament, pad }">
    <slot />
  </section>
</template>

<style scoped>
.ow-pane {
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-pane);
  box-shadow: var(--ow2-shadow);
  position: relative;
}
.ow-pane.pad {
  padding: 16px 18px;
}
.ow-pane.ornament::before {
  content: "";
  position: absolute;
  top: 6px;
  left: 6px;
  right: 6px;
  bottom: 6px;
  border-radius: calc(var(--ow2-radius-pane) - 4px);
  border: 1px solid rgba(192, 160, 98, 0.14);
  pointer-events: none;
}
</style>
