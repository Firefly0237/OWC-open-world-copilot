<script setup lang="ts">
import { computed, ref } from "vue";
import {
  type GraphEdge,
  type GraphNode,
  FLAG_COLOR,
  GEM_FAMILIES,
  kindColor,
  kindFamily,
  kindGlyph,
} from "../graphTheme";

const props = withDefaults(
  defineProps<{
    nodes: GraphNode[];
    edges: GraphEdge[];
    nodeRadius?: number;
    selected?: string;
    draggable?: boolean;
    shape?: "gem" | "card";
  }>(),
  { nodeRadius: 24, selected: "", draggable: false, shape: "gem" },
);
const emit = defineEmits<{ select: [ref: string]; move: [ref: string, x: number, y: number] }>();

const svgEl = ref<SVGSVGElement | null>(null);
const hovered = ref("");
const dragRef = ref("");
const dragPos = ref<{ x: number; y: number } | null>(null);

const R = computed(() => props.nodeRadius);
const CARD_W = 132;
const CARD_H = 46;

const families = Object.keys(GEM_FAMILIES);

function px(node: GraphNode): number {
  return dragRef.value === node.ref && dragPos.value ? dragPos.value.x : node.x;
}
function py(node: GraphNode): number {
  return dragRef.value === node.ref && dragPos.value ? dragPos.value.y : node.y;
}

const byRef = computed(() => new Map(props.nodes.map((n) => [n.ref, n])));

const viewBox = computed(() => {
  if (!props.nodes.length) return "0 0 600 320";
  const pad = (props.shape === "card" ? CARD_W / 2 : R.value) + 34;
  const xs = props.nodes.map(px);
  const ys = props.nodes.map(py);
  const minX = Math.min(...xs) - pad;
  const minY = Math.min(...ys) - pad;
  return `${minX} ${minY} ${Math.max(...xs) - minX + pad} ${Math.max(...ys) - minY + pad}`;
});

interface DrawnEdge {
  d: string;
  mx: number;
  my: number;
  label: string;
  symmetric: boolean;
  on: boolean;
}

const drawnEdges = computed<DrawnEdge[]>(() => {
  const out: DrawnEdge[] = [];
  for (const e of props.edges) {
    const s = byRef.value.get(e.source);
    const t = byRef.value.get(e.target);
    if (!s || !t) continue;
    const x1 = px(s);
    const y1 = py(s);
    const x2 = px(t);
    const y2 = py(t);
    // gentle perpendicular bow so reciprocal links don't overlap and the web reads as constellation
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = Math.hypot(dx, dy) || 1;
    const bow = Math.min(28, len * 0.12);
    const cx = (x1 + x2) / 2 + (-dy / len) * bow;
    const cy = (y1 + y2) / 2 + (dx / len) * bow;
    out.push({
      d: `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`,
      mx: (x1 + x2) / 2 + (-dy / len) * bow * 0.5,
      my: (y1 + y2) / 2 + (dx / len) * bow * 0.5,
      label: e.label ?? e.kind ?? "",
      symmetric: !!e.symmetric,
      on: hovered.value === e.source || hovered.value === e.target,
    });
  }
  return out;
});

function ringColor(n: GraphNode): string {
  if (n.flag && FLAG_COLOR[n.flag]) return FLAG_COLOR[n.flag];
  if (n.ref === props.selected) return "var(--ow-gold-bright)";
  if (n.focus) return "var(--ow-gold-bright)";
  return kindColor(n.kind);
}


function toSvgPoint(evt: PointerEvent): { x: number; y: number } | null {
  const svg = svgEl.value;
  if (!svg) return null;
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const p = pt.matrixTransform(ctm.inverse());
  return { x: Math.round(p.x * 10) / 10, y: Math.round(p.y * 10) / 10 };
}

function onPointerDown(node: GraphNode, evt: PointerEvent): void {
  // keep an enclosing ZoomCanvas from treating a node interaction as a background pan
  evt.stopPropagation();
  emit("select", node.ref);
  if (!props.draggable) return;
  evt.preventDefault();
  dragRef.value = node.ref;
  dragPos.value = { x: node.x, y: node.y };
  (evt.target as Element).setPointerCapture?.(evt.pointerId);
}
function onPointerMove(evt: PointerEvent): void {
  if (!dragRef.value) return;
  const p = toSvgPoint(evt);
  if (p) dragPos.value = p;
}
function onPointerUp(): void {
  if (dragRef.value && dragPos.value) {
    emit("move", dragRef.value, dragPos.value.x, dragPos.value.y);
  }
  dragRef.value = "";
  dragPos.value = null;
}
</script>

<template>
  <svg
    ref="svgEl"
    :viewBox="viewBox"
    class="svg-graph"
    role="img"
    aria-label="关系 / 流程星图"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointerleave="onPointerUp"
  >
    <defs>
      <radialGradient
        v-for="fam in families"
        :id="`gem-${fam}`"
        :key="fam"
        cx="42%"
        cy="36%"
        r="68%"
      >
        <stop offset="0" :stop-color="GEM_FAMILIES[fam][0]" />
        <stop offset="46%" :stop-color="GEM_FAMILIES[fam][1]" />
        <stop offset="100%" :stop-color="GEM_FAMILIES[fam][2]" />
      </radialGradient>
      <radialGradient id="sg-halo" cx="50%" cy="50%" r="50%">
        <stop offset="0" stop-color="#f0d28a" stop-opacity="0.5" />
        <stop offset="100%" stop-color="#f0d28a" stop-opacity="0" />
      </radialGradient>
      <marker id="sg-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 Z" fill="#5a6a9a" />
      </marker>
    </defs>

    <g class="edges">
      <g v-for="(e, i) in drawnEdges" :key="i" :class="{ on: e.on }">
        <path
          :d="e.d"
          class="edge"
          fill="none"
          :marker-end="e.symmetric ? undefined : 'url(#sg-arrow)'"
        />
        <path :d="e.d" class="edge-flow" fill="none" />
        <text v-if="e.label" :x="e.mx" :y="e.my - 4" class="edge-label">{{ e.label }}</text>
      </g>
    </g>

    <g class="nodes">
      <g
        v-for="n in nodes"
        :key="n.ref"
        :transform="`translate(${px(n)} ${py(n)})`"
        class="node"
        :class="{
          sel: n.ref === selected,
          dim: hovered && hovered !== n.ref,
          drag: dragRef === n.ref,
          grab: draggable,
        }"
        @pointerdown="onPointerDown(n, $event)"
        @mouseenter="hovered = n.ref"
        @mouseleave="hovered = ''"
      >
        <!-- selection emphasis -->
        <template v-if="n.ref === selected || n.focus">
          <circle :r="R + 16" fill="url(#sg-halo)" />
          <circle
            class="sel-ring"
            :r="R + 8"
            fill="none"
            :stroke="ringColor(n)"
            stroke-opacity="0.8"
            stroke-width="1.2"
            stroke-dasharray="3 5"
          />
          <g :stroke="ringColor(n)" stroke-opacity="0.75" stroke-width="1.2">
            <line :x1="0" :y1="-(R + 12)" :x2="0" :y2="-(R + 18)" />
            <line :x1="R + 12" :y1="0" :x2="R + 18" :y2="0" />
            <line :x1="0" :y1="R + 12" :x2="0" :y2="R + 18" />
            <line :x1="-(R + 12)" :y1="0" :x2="-(R + 18)" :y2="0" />
          </g>
        </template>

        <!-- card shape (Detroit-style dialogue node) -->
        <template v-if="shape === 'card'">
          <rect
            :x="-CARD_W / 2"
            :y="-CARD_H / 2"
            :width="CARD_W"
            :height="CARD_H"
            rx="8"
            :fill="kindColor(n.kind)"
            fill-opacity="0.14"
            :stroke="ringColor(n)"
            :stroke-width="n.ref === selected ? 2 : 1.2"
          />
          <rect
            :x="-CARD_W / 2"
            :y="-CARD_H / 2"
            width="4"
            :height="CARD_H"
            rx="2"
            :fill="kindColor(n.kind)"
          />
          <text class="card-label" text-anchor="middle" y="-2">{{ n.label }}</text>
          <text v-if="n.sublabel" class="card-sub" text-anchor="middle" y="13">{{ n.sublabel }}</text>
        </template>

        <!-- circular "star位" node: gradient core + type-colour frame ring + type glyph + glow -->
        <template v-else>
          <!-- outer glow ring -->
          <circle
            :r="R + 2.5"
            fill="none"
            :stroke="ringColor(n)"
            stroke-opacity="0.22"
            stroke-width="3.5"
            class="node-glow"
          />
          <!-- core -->
          <circle
            :r="R"
            :fill="`url(#gem-${kindFamily(n.kind)})`"
            :stroke="ringColor(n)"
            :stroke-width="n.ref === selected || n.focus || n.flag ? 2.4 : 1.5"
            class="node-core"
          />
          <!-- inner frame ring -->
          <circle :r="R * 0.66" fill="none" stroke="rgba(255,250,235,0.22)" stroke-width="0.8" />
          <!-- type glyph (embossed) -->
          <path :d="kindGlyph(n.kind)" :transform="`scale(${R * 0.4})`" class="node-glyph" />
          <!-- specular highlight -->
          <circle :cx="-R * 0.32" :cy="-R * 0.32" :r="R * 0.2" class="node-spec" />
          <text class="label" text-anchor="middle" :y="R + 15">{{ n.label }}</text>
          <text v-if="n.sublabel" class="sublabel" text-anchor="middle" :y="R + 26">
            {{ n.sublabel }}
          </text>
        </template>
      </g>
    </g>
  </svg>
</template>

<style scoped>
.svg-graph {
  width: 100%;
  height: auto;
  display: block;
  user-select: none;
  touch-action: none;
}

.edge {
  stroke: #5a6a9a;
  stroke-width: 1.2;
}
.edges .on .edge {
  stroke: var(--ow-cyan);
  stroke-width: 1.8;
}
/* a faint energy pulse travelling each link — lights up on hover */
.edge-flow {
  stroke: rgba(143, 214, 232, 0.5);
  stroke-width: 1.4;
  stroke-dasharray: 2 14;
  opacity: 0;
  animation: edge-flow 1.4s linear infinite;
}
.edges .on .edge-flow {
  opacity: 1;
}
@keyframes edge-flow {
  to {
    stroke-dashoffset: -16;
  }
}
.edge-label {
  fill: var(--ow-muted);
  font-size: 10px;
  text-anchor: middle;
}

.node {
  transition: opacity 0.15s ease;
}
.node.grab {
  cursor: grab;
}
.node.drag {
  cursor: grabbing;
}
.node.dim {
  opacity: 0.4;
}

.node-glyph {
  fill: rgba(8, 11, 28, 0.5);
  pointer-events: none;
}
.node-spec {
  fill: rgba(255, 250, 235, 0.45);
  pointer-events: none;
}
.node-core {
  filter: drop-shadow(0 1px 3px rgba(4, 7, 22, 0.5));
}
.node.sel .node-core,
.node:hover .node-core {
  filter: drop-shadow(0 0 8px rgba(240, 210, 138, 0.5));
}
.node-glow {
  opacity: 0.7;
  transition: opacity 0.18s ease;
}
.node.sel .node-glow,
.node:hover .node-glow {
  opacity: 1;
}

.label {
  fill: var(--ow-ink);
  font-size: 11px;
  font-family: var(--ow-serif);
}
.sublabel {
  fill: var(--ow-muted);
  font-size: 8.5px;
}
.card-label {
  fill: var(--ow-ink);
  font-size: 11px;
}
.card-sub {
  fill: var(--ow-muted);
  font-size: 9px;
}

.sel-ring {
  animation: sg-spin 22s linear infinite;
  transform-origin: center;
  transform-box: fill-box;
}

@keyframes sg-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .node,
  .sel-ring,
  .edge-flow {
    transition: none;
    animation: none;
  }
  .edge-flow {
    display: none;
  }
}
</style>
