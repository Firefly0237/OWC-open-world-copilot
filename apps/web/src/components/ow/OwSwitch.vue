<script setup lang="ts">
const props = withDefaults(defineProps<{ modelValue: boolean; disabled?: boolean }>(), {
  disabled: false,
});
const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
function toggle(): void {
  if (props.disabled) return;
  emit("update:modelValue", !props.modelValue);
}
</script>

<template>
  <button
    type="button"
    class="ow-switch"
    :class="{ on: modelValue }"
    role="switch"
    :aria-checked="modelValue"
    :disabled="disabled"
    @click="toggle"
  />
</template>

<style scoped>
.ow-switch {
  width: 40px;
  height: 22px;
  border-radius: 11px;
  border: none;
  background: rgba(120, 98, 60, 0.22);
  position: relative;
  cursor: pointer;
  transition: background var(--ow2-dur);
  padding: 0;
  flex: none;
}
.ow-switch::after {
  content: "";
  position: absolute;
  top: 2.5px;
  left: 3px;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: var(--ow2-card);
  box-shadow: 0 1px 4px rgba(120, 98, 60, 0.35);
  transition: transform 0.22s var(--ow2-ease-spring);
}
.ow-switch.on {
  background: var(--ow2-teal);
}
.ow-switch.on::after {
  transform: translateX(17px);
}
.ow-switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--ow2-gold-soft);
}
.ow-switch:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
