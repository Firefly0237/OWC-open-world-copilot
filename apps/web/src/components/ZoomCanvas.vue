<script setup lang="ts">
// A zoom / pan / fullscreen shell for the graph + timeline views, which were too small to read.
// Wheel or +/- to zoom, drag the background to pan, and "放大" expands to a near-fullscreen overlay
// with a sci-fi zoom-in. Interactive children (nodes that drag themselves) call stopPropagation on
// pointerdown, so panning only kicks in on empty space.
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";

const props = withDefaults(
  defineProps<{ min?: number; max?: number; height?: string; natural?: boolean }>(),
  {
    min: 0.5,
    max: 3,
    height: "clamp(360px, 56vh, 640px)",
    // natural = render slotted content at its intrinsic size and pan over it (for wide content like
    // the timeline, which the default fit-to-viewport would shrink to an unreadable strip)
    natural: false,
  },
);

const zoom = ref(1);
const panX = ref(0);
const panY = ref(0);
const full = ref(false);
const root = ref<HTMLElement | null>(null);
const vp = ref<HTMLElement | null>(null);
const fullButton = ref<HTMLButtonElement | null>(null);

let dragging = false;
let sx = 0;
let sy = 0;
let ox = 0;
let oy = 0;
let fullReturnFocus: HTMLElement | null = null;

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const pct = computed(() => Math.round(zoom.value * 100));
const transform = computed(
  () => `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
);

function clampZoom(z: number): number {
  return Math.min(props.max, Math.max(props.min, z));
}
function zoomBy(factor: number): void {
  zoom.value = clampZoom(zoom.value * factor);
}
function reset(): void {
  zoom.value = 1;
  panX.value = 0;
  panY.value = 0;
}
function onWheel(e: WheelEvent): void {
  zoomBy(e.deltaY < 0 ? 1.12 : 0.89);
}
function onDown(e: PointerEvent): void {
  dragging = true;
  sx = e.clientX;
  sy = e.clientY;
  ox = panX.value;
  oy = panY.value;
  vp.value?.setPointerCapture?.(e.pointerId);
}
function onMove(e: PointerEvent): void {
  if (!dragging) return;
  panX.value = ox + (e.clientX - sx);
  panY.value = oy + (e.clientY - sy);
}
function onUp(e: PointerEvent): void {
  dragging = false;
  vp.value?.releasePointerCapture?.(e.pointerId);
}
function setFull(next: boolean): void {
  if (full.value === next) return;
  if (next) {
    fullReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : fullButton.value;
    full.value = true;
    document.body.classList.add("zoom-canvas-fullscreen-open");
    reset();
    void nextTick(() => fullButton.value?.focus({ preventScroll: true }));
    return;
  }
  const target = fullReturnFocus?.isConnected ? fullReturnFocus : fullButton.value;
  full.value = false;
  document.body.classList.remove("zoom-canvas-fullscreen-open");
  fullReturnFocus = null;
  reset();
  void nextTick(() => target?.focus({ preventScroll: true }));
}
function toggleFull(): void {
  setFull(!full.value);
}
function onSlider(e: Event): void {
  zoom.value = clampZoom(Number((e.target as HTMLInputElement).value) / 100);
}
function onKey(e: KeyboardEvent): void {
  if (!full.value || document.querySelector(".modal-root")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopImmediatePropagation();
    setFull(false);
    return;
  }
  if (e.key !== "Tab" || !root.value) return;
  const focusable = [...root.value.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter((item) => item.offsetParent !== null);
  if (!focusable.length) {
    e.preventDefault();
    e.stopImmediatePropagation();
    root.value.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (!root.value.contains(active)) {
    e.preventDefault();
    e.stopImmediatePropagation();
    (e.shiftKey ? last : first).focus();
  } else if (e.shiftKey && active === first) {
    e.preventDefault();
    e.stopImmediatePropagation();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    e.stopImmediatePropagation();
    first.focus();
  }
}
onMounted(() => window.addEventListener("keydown", onKey, true));
onUnmounted(() => {
  window.removeEventListener("keydown", onKey, true);
  document.body.classList.remove("zoom-canvas-fullscreen-open");
});
</script>

<template>
  <!-- when full, teleport to <body> so position:fixed escapes any filtered/transformed ancestor
       (the .pane drop-shadow filter would otherwise trap the overlay inside the panel) -->
  <Teleport to="body" :disabled="!full">
   <div
     ref="root"
     class="zc"
     :class="{ full }"
     :role="full ? 'dialog' : undefined"
     :aria-modal="full || undefined"
     :aria-label="full ? '可缩放画布全屏视图' : undefined"
     :tabindex="full ? -1 : undefined"
   >
    <div
      ref="vp"
      class="zc-vp"
      :class="{ dragging }"
      :style="full ? {} : { height }"
      @wheel.prevent="onWheel"
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointerleave="onUp"
    >
      <div class="zc-content" :class="{ natural }" :style="{ transform }"><slot /></div>
    </div>

    <div class="zc-bar" @pointerdown.stop>
      <button type="button" class="zc-btn" title="缩小" aria-label="缩小画布" @click="zoomBy(0.83)">−</button>
      <input
        class="zc-range"
        type="range"
        aria-label="画布缩放比例"
        :aria-valuetext="`${pct}%`"
        :min="Math.round(min * 100)"
        :max="Math.round(max * 100)"
        :value="pct"
        @input="onSlider"
      />
      <button type="button" class="zc-btn" title="放大" aria-label="放大画布" @click="zoomBy(1.2)">+</button>
      <span class="zc-pct num">{{ pct }}%</span>
      <button type="button" class="zc-btn wide" @click="reset">复位</button>
      <button
        ref="fullButton"
        type="button"
        class="zc-btn wide primary"
        :aria-label="full ? '退出画布全屏视图' : '打开画布全屏视图'"
        :aria-pressed="full"
        @click="toggleFull"
      >{{ full ? "退出全屏" : "放大视图" }}</button>
    </div>
   </div>
  </Teleport>
</template>

<style scoped>
.zc {
  position: relative;
}
.zc.full {
  position: fixed;
  inset: 2.5vh 2vw;
  z-index: 7000;
  display: flex;
  flex-direction: column;
  background: linear-gradient(168deg, rgba(22, 29, 68, 0.97), rgba(10, 14, 36, 0.98));
  border: 1px solid var(--ow-edge-violet);
  border-radius: 10px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.6), inset 0 1px 0 rgba(240, 210, 138, 0.12);
  padding: 0.6rem;
  animation: zc-zoom-in 0.34s cubic-bezier(0.16, 0.84, 0.24, 1);
}
:global(body.zoom-canvas-fullscreen-open) {
  overflow: hidden;
}
@keyframes zc-zoom-in {
  from {
    opacity: 0;
    transform: scale(0.94);
    filter: blur(4px);
  }
  to {
    opacity: 1;
    transform: scale(1);
    filter: blur(0);
  }
}

.zc-vp {
  position: relative;
  overflow: hidden;
  border-radius: 8px;
  border: 1px solid var(--ow-line);
  background:
    radial-gradient(60% 60% at 80% 12%, rgba(160, 138, 255, 0.06), transparent 70%),
    var(--ow-panel-2);
  cursor: grab;
  touch-action: none;
}
.zc.full .zc-vp {
  flex: 1;
}
.zc-vp.dragging {
  cursor: grabbing;
}
.zc-content {
  width: 100%;
  height: 100%;
  transform-origin: center center;
  transition: transform 0.08s linear;
}
/* natural mode: size to content (intrinsic), anchor top-left, pan over it */
.zc-content.natural {
  width: max-content;
  height: max-content;
  transform-origin: top left;
}
.zc-vp.dragging .zc-content {
  transition: none;
}
.zc-content :deep(svg) {
  display: block;
}
/* fit mode scales the svg to fill the viewport (square-ish content like the graph) */
.zc-content:not(.natural) :deep(svg) {
  width: 100%;
  height: 100%;
}

.zc-bar {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-top: 0.5rem;
  flex-wrap: wrap;
}
.zc-btn {
  min-width: 44px;
  height: 44px;
  padding: 0 0.5rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-panel-2);
  color: var(--ow-ink);
  font: inherit;
  font-size: 0.9rem;
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
}
.zc-btn.wide {
  font-size: 0.8rem;
}
.zc-btn:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}
.zc-btn.primary {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
}
.zc-btn.primary:hover {
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.22);
}
.zc-range {
  width: clamp(96px, 20vw, 220px);
  min-height: 44px;
  accent-color: var(--ow-gold);
}
.zc-pct {
  display: inline-grid;
  min-height: 44px;
  place-items: center;
  font-size: 0.78rem;
  color: var(--ow-cyan);
  min-width: 3em;
  text-align: center;
}
@media (prefers-reduced-motion: reduce) {
  .zc.full {
    animation: none;
  }
  .zc-content {
    transition: none;
  }
}
</style>
