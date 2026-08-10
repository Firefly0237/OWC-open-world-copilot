<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

export interface OwSelectOption {
  value: string;
  label: string;
  hint?: string;
}

const props = withDefaults(
  defineProps<{
    options: OwSelectOption[];
    modelValue: string;
    placeholder?: string;
    disabled?: boolean;
  }>(),
  { placeholder: "请选择…", disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

const open = ref(false);
const active = ref(-1);
const root = ref<HTMLElement | null>(null);
const current = computed(() => props.options.find((o) => o.value === props.modelValue));

function toggle(): void {
  if (props.disabled) return;
  open.value = !open.value;
  if (open.value) {
    const idx = props.options.findIndex((o) => o.value === props.modelValue);
    active.value = idx >= 0 ? idx : 0;
  }
}
function pick(option: OwSelectOption): void {
  emit("update:modelValue", option.value);
  open.value = false;
  root.value?.querySelector<HTMLButtonElement>(".ow-sel-trigger")?.focus();
}
function onKeydown(event: KeyboardEvent): void {
  if (props.disabled) return;
  if (!open.value) {
    if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      toggle();
    }
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    open.value = false;
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    active.value = Math.min(props.options.length - 1, active.value + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    active.value = Math.max(0, active.value - 1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    const hit = props.options[active.value];
    if (hit) pick(hit);
  } else if (event.key === "Tab") {
    open.value = false;
  }
}
function onDocPointer(event: Event): void {
  if (root.value && !root.value.contains(event.target as Node)) open.value = false;
}
onMounted(() => document.addEventListener("pointerdown", onDocPointer));
onBeforeUnmount(() => document.removeEventListener("pointerdown", onDocPointer));
</script>

<template>
  <div ref="root" class="ow-sel" :class="{ open, 'is-disabled': disabled }" @keydown="onKeydown">
    <button
      type="button"
      class="ow-sel-trigger"
      :disabled="disabled"
      aria-haspopup="listbox"
      :aria-expanded="open"
      @click="toggle"
    >
      <span :class="{ 'is-placeholder': !current }">{{ current ? current.label : placeholder }}</span>
      <i class="ow-sel-caret" aria-hidden="true" />
    </button>
    <Transition name="ow-sel-pop">
      <ul v-if="open" class="ow-sel-list" role="listbox" :aria-activedescendant="`ow-sel-opt-${active}`">
        <li
          v-for="(option, index) in options"
          :id="`ow-sel-opt-${index}`"
          :key="option.value"
          class="ow-sel-opt"
          :class="{ active: index === active, selected: option.value === modelValue }"
          role="option"
          :aria-selected="option.value === modelValue"
          @pointerenter="active = index"
          @click="pick(option)"
        >
          <i class="ow-sel-diamond" aria-hidden="true" />
          <span class="ow-sel-label">{{ option.label }}</span>
          <span v-if="option.hint" class="ow-sel-hint">{{ option.hint }}</span>
        </li>
      </ul>
    </Transition>
  </div>
</template>

<style scoped>
.ow-sel {
  position: relative;
  width: 100%;
}
.ow-sel-trigger {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background: var(--ow2-field);
  border: 1px solid var(--ow2-line);
  border-radius: var(--ow2-radius-control);
  padding: 8px 12px;
  font-size: 12.5px;
  font-family: var(--ow2-sans);
  color: var(--ow2-ink);
  cursor: pointer;
  transition: border-color var(--ow2-dur), box-shadow var(--ow2-dur), background var(--ow2-dur);
}
.ow-sel-trigger:hover:not(:disabled) {
  border-color: var(--ow2-gold-soft);
}
.ow-sel-trigger:focus-visible,
.open .ow-sel-trigger {
  outline: none;
  border-color: var(--ow2-gold);
  box-shadow: 0 0 0 3px var(--ow2-gold-faint);
  background: var(--ow2-card);
}
.ow-sel-trigger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.is-placeholder {
  color: var(--ow2-muted);
}
.ow-sel-caret {
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--ow2-gold-deep);
  border-bottom: 1.5px solid var(--ow2-gold-deep);
  transform: rotate(45deg) translateY(-2px);
  transition: transform var(--ow2-dur);
  flex: none;
}
.open .ow-sel-caret {
  transform: rotate(225deg) translateY(-2px);
}
.ow-sel-list {
  position: absolute;
  z-index: 40;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  margin: 0;
  padding: 5px;
  list-style: none;
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-pane);
  box-shadow: var(--ow2-shadow-lift);
  max-height: 260px;
  overflow: auto;
}
.ow-sel-opt {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 10px;
  border-radius: var(--ow2-radius-chip);
  font-size: 12.5px;
  color: var(--ow2-ink-soft);
  cursor: pointer;
}
.ow-sel-diamond {
  width: 6px;
  height: 6px;
  transform: rotate(45deg);
  border: 1px solid var(--ow2-muted);
  flex: none;
  transition: all var(--ow2-dur);
}
.ow-sel-opt.active {
  background: var(--ow2-gold-faint);
  color: var(--ow2-ink);
}
.ow-sel-opt.selected {
  color: var(--ow2-gold-deep);
  font-weight: 600;
}
.ow-sel-opt.selected .ow-sel-diamond {
  background: var(--ow2-gold);
  border-color: var(--ow2-gold);
}
.ow-sel-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ow-sel-hint {
  font-size: 10.5px;
  color: var(--ow2-muted);
}
.ow-sel-pop-enter-active,
.ow-sel-pop-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.ow-sel-pop-enter-from,
.ow-sel-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
