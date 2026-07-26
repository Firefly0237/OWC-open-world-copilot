<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

export interface OwEntityOption {
  id: string;
  label: string;
  type: string;
}

const props = withDefaults(
  defineProps<{
    options: OwEntityOption[];
    modelValue: string;
    placeholder?: string;
    recentIds?: string[];
    disabled?: boolean;
  }>(),
  { placeholder: "搜索并选择实体…", recentIds: () => [], disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [id: string] }>();

const open = ref(false);
const query = ref("");
const active = ref(0);
const root = ref<HTMLElement | null>(null);

const current = computed(() => props.options.find((o) => o.id === props.modelValue));
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return props.options;
  return props.options.filter(
    (o) => o.label.toLowerCase().includes(q) || o.id.toLowerCase().includes(q) || o.type.toLowerCase().includes(q),
  );
});
const groups = computed(() => {
  const result: { name: string; items: OwEntityOption[] }[] = [];
  const push = (name: string, items: OwEntityOption[]): void => {
    if (items.length) result.push({ name, items });
  };
  if (!query.value.trim() && props.recentIds.length) {
    push("最近使用", props.recentIds.map((id) => props.options.find((o) => o.id === id)).filter((o): o is OwEntityOption => Boolean(o)));
  }
  const byType = new Map<string, OwEntityOption[]>();
  for (const option of filtered.value) {
    const list = byType.get(option.type) ?? [];
    list.push(option);
    byType.set(option.type, list);
  }
  for (const [type, items] of byType) push(type, items);
  return result;
});
const flat = computed(() => groups.value.flatMap((g) => g.items));

function toggle(): void {
  if (props.disabled) return;
  open.value = !open.value;
  if (open.value) {
    query.value = "";
    active.value = 0;
  }
}
function pick(option: OwEntityOption): void {
  emit("update:modelValue", option.id);
  open.value = false;
}
function onKeydown(event: KeyboardEvent): void {
  if (!open.value) return;
  if (event.key === "Escape") {
    open.value = false;
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    active.value = Math.min(flat.value.length - 1, active.value + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    active.value = Math.max(0, active.value - 1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    const hit = flat.value[active.value];
    if (hit) pick(hit);
  }
}
function onDocPointer(event: Event): void {
  if (root.value && !root.value.contains(event.target as Node)) open.value = false;
}
onMounted(() => document.addEventListener("pointerdown", onDocPointer));
onBeforeUnmount(() => document.removeEventListener("pointerdown", onDocPointer));
function isActive(option: OwEntityOption): boolean {
  return flat.value[active.value]?.id === option.id;
}
</script>

<template>
  <div ref="root" class="ow-ep" :class="{ open, 'is-disabled': disabled }" @keydown="onKeydown">
    <button type="button" class="ow-ep-trigger" :disabled="disabled" :aria-expanded="open" @click="toggle">
      <span v-if="current" class="ow-ep-current">
        <i class="ow-ep-diamond on" aria-hidden="true" />{{ current.label }}
        <em>{{ current.type }}</em>
      </span>
      <span v-else class="ow-ep-placeholder">{{ placeholder }}</span>
      <i class="ow-ep-caret" aria-hidden="true" />
    </button>
    <Transition name="ow-ep-pop">
      <div v-if="open" class="ow-ep-panel">
        <input v-model="query" class="ow-ep-search" placeholder="输入名称 / ID / 类型过滤…" />
        <div class="ow-ep-list">
          <div v-for="group in groups" :key="group.name" class="ow-ep-group">
            <p class="ow-ep-group-name">{{ group.name }}</p>
            <button
              v-for="option in group.items"
              :key="option.id"
              type="button"
              class="ow-ep-opt"
              :class="{ active: isActive(option), selected: option.id === modelValue }"
              @click="pick(option)"
            >
              <i class="ow-ep-diamond" aria-hidden="true" />
              <span class="ow-ep-name">{{ option.label }}</span>
              <span class="ow-ep-id">{{ option.id }}</span>
            </button>
          </div>
          <p v-if="!flat.length" class="ow-ep-none">没有匹配的实体</p>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.ow-ep {
  position: relative;
  width: 100%;
}
.ow-ep-trigger {
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
.ow-ep-trigger:hover:not(:disabled) { border-color: var(--ow2-gold-soft); }
.ow-ep-trigger:focus-visible,
.open .ow-ep-trigger {
  outline: none;
  border-color: var(--ow2-gold);
  box-shadow: 0 0 0 3px var(--ow2-gold-faint);
  background: var(--ow2-card);
}
.ow-ep-current {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.ow-ep-current em {
  font-style: normal;
  font-size: 10.5px;
  color: var(--ow2-muted);
}
.ow-ep-placeholder { color: var(--ow2-muted); }
.ow-ep-caret {
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--ow2-gold-deep);
  border-bottom: 1.5px solid var(--ow2-gold-deep);
  transform: rotate(45deg) translateY(-2px);
  flex: none;
}
.ow-ep-panel {
  position: absolute;
  z-index: 40;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  background: var(--ow2-card);
  border: 1px solid var(--ow2-gold-soft);
  border-radius: var(--ow2-radius-pane);
  box-shadow: var(--ow2-shadow-lift);
  overflow: hidden;
}
.ow-ep-search {
  width: 100%;
  border: none;
  border-bottom: 1px solid var(--ow2-line);
  background: var(--ow2-field);
  padding: 9px 12px;
  font-size: 12.5px;
  font-family: var(--ow2-sans);
  color: var(--ow2-ink);
  outline: none;
}
.ow-ep-list {
  max-height: 260px;
  overflow: auto;
  padding: 5px;
}
.ow-ep-group-name {
  font-size: 10.5px;
  color: var(--ow2-gold-deep);
  letter-spacing: 0.08em;
  margin: 6px 8px 3px;
}
.ow-ep-opt {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 10px;
  border: none;
  border-radius: var(--ow2-radius-chip);
  background: none;
  font-size: 12.5px;
  font-family: var(--ow2-sans);
  color: var(--ow2-ink-soft);
  cursor: pointer;
  text-align: left;
}
.ow-ep-diamond {
  width: 6px;
  height: 6px;
  transform: rotate(45deg);
  border: 1px solid var(--ow2-muted);
  flex: none;
}
.ow-ep-diamond.on {
  background: var(--ow2-gold);
  border-color: var(--ow2-gold);
}
.ow-ep-opt.active { background: var(--ow2-gold-faint); color: var(--ow2-ink); }
.ow-ep-opt.selected { color: var(--ow2-gold-deep); font-weight: 600; }
.ow-ep-opt.selected .ow-ep-diamond { background: var(--ow2-gold); border-color: var(--ow2-gold); }
.ow-ep-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ow-ep-id { font-size: 10px; color: var(--ow2-muted); font-family: var(--ow2-num); }
.ow-ep-none { padding: 14px; text-align: center; font-size: 12px; color: var(--ow2-muted); }
.ow-ep-pop-enter-active,
.ow-ep-pop-leave-active { transition: opacity 0.15s ease, transform 0.15s ease; }
.ow-ep-pop-enter-from,
.ow-ep-pop-leave-to { opacity: 0; transform: translateY(-4px); }
</style>
