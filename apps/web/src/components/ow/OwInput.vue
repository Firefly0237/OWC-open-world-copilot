<script setup lang="ts">
withDefaults(
  defineProps<{
    modelValue: string;
    placeholder?: string;
    maxlength?: number;
    required?: boolean;
    disabled?: boolean;
    type?: string;
  }>(),
  { placeholder: "", maxlength: undefined, required: false, disabled: false, type: "text" },
);
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
function onInput(event: Event): void {
  emit("update:modelValue", (event.target as HTMLInputElement).value);
}
</script>

<template>
  <input
    class="ow-input"
    :class="{ 'is-disabled': disabled }"
    :value="modelValue"
    :placeholder="placeholder"
    :maxlength="maxlength"
    :required="required"
    :disabled="disabled"
    :type="type"
    @input="onInput"
  />
</template>

<style scoped>
.ow-input {
  background: var(--ow2-field);
  border: 1px solid var(--ow2-line);
  border-radius: var(--ow2-radius-control);
  padding: 8px 12px;
  font-size: 12.5px;
  font-family: var(--ow2-sans);
  color: var(--ow2-ink);
  outline: none;
  width: 100%;
  transition: border-color var(--ow2-dur), box-shadow var(--ow2-dur), background var(--ow2-dur);
}
.ow-input::placeholder {
  color: var(--ow2-muted);
}
.ow-input:hover:not(:disabled) {
  border-color: var(--ow2-gold-soft);
}
.ow-input:focus {
  border-color: var(--ow2-gold);
  box-shadow: 0 0 0 3px var(--ow2-gold-faint);
  background: var(--ow2-card);
}
.ow-input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
