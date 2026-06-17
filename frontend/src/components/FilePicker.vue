<script setup lang="ts">
// Themed file input: the native <input type=file> "选择文件" button clashes with everything; this
// wraps it in a cut-corner gold button + filename readout + drag-and-drop zone. Emits the File;
// the parent reads it however it needs (base64 / text).
import { ref } from "vue";

defineProps<{ accept?: string; hint?: string }>();
const emit = defineEmits<{ select: [file: File] }>();

const fileName = ref("");
const over = ref(false);

function take(file: File | undefined | null): void {
  if (!file) return;
  fileName.value = file.name;
  emit("select", file);
}
function onPick(e: Event): void {
  take((e.target as HTMLInputElement).files?.[0]);
}
function onDrop(e: DragEvent): void {
  over.value = false;
  take(e.dataTransfer?.files?.[0]);
}
</script>

<template>
  <label
    class="fp"
    :class="{ over, has: !!fileName }"
    @dragover.prevent="over = true"
    @dragleave="over = false"
    @drop.prevent="onDrop"
  >
    <input type="file" class="fp-native" :accept="accept" @change="onPick" />
    <span class="fp-btn">
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 4v10m0 0 4-4m-4 4-4-4" />
        <path d="M5 17v2h14v-2" />
      </svg>
      选择文件
    </span>
    <span class="fp-name" :class="{ empty: !fileName }">{{ fileName || hint || "未选择，也可拖入文件" }}</span>
  </label>
</template>

<style scoped>
.fp {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.4rem 0.45rem 0.4rem 0.4rem;
  border: 1px dashed var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  cursor: pointer;
  transition: border-color 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
.fp:hover {
  border-color: var(--ow-gold-soft);
}
.fp.over {
  border-color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  box-shadow: 0 0 14px rgba(240, 210, 138, 0.2) inset;
}
.fp.has {
  border-style: solid;
  border-color: var(--ow-gold-soft);
}
.fp-native {
  display: none;
}
.fp-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.82rem;
  font-weight: 600;
  color: #241a05;
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  padding: 0.34rem 0.7rem;
  /* the HSR cut corner */
  clip-path: polygon(8px 0, 100% 0, 100% calc(100% - 8px), calc(100% - 8px) 100%, 0 100%, 0 8px);
}
.fp:hover .fp-btn {
  filter: brightness(1.06);
}
.fp-name {
  min-width: 0;
  font-size: 0.82rem;
  color: var(--ow-ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fp-name.empty {
  color: var(--ow-muted);
}
</style>
