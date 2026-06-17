<script setup lang="ts">
// True 3D relationship graph (WebGL via 3d-force-graph / three.js). Nodes are depth-lit spheres
// coloured by type with always-on labels; links are coloured + arrowed + particle-flowed by relation
// CATEGORY (所属 gold arrows / 人际 cyan flow / 阵营邦交 violet flow / 地理 green / 叙事 amber) so the
// graph finally distinguishes membership from interpersonal. Orbit to rotate, scroll to zoom, click a
// node to open its detail. The view layout is physics-driven; canonical data is untouched.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as THREE from "three";
import ForceGraph3DImport from "3d-force-graph";
import SpriteTextImport from "three-spritetext";
import { type GraphEdge, type GraphNode, EDGE_CAT_META, edgeCat, kindColor } from "../graphTheme";

// the viz libraries are loosely typed and kapsule-chained; drive them through any-typed handles
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const ForceGraph3D = ForceGraph3DImport as any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const SpriteText = SpriteTextImport as any;

// minimalist sci-fi node: a thin ring + bright core drawn to a canvas, used as a camera-facing
// sprite — reads as a clean star-map waypoint (not a fat opaque marble) while still depth-cued.
function hexA(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}
function makeReticle(color: string, sel: boolean) {
  const S = 128;
  const cv = document.createElement("canvas");
  cv.width = cv.height = S;
  const ctx = cv.getContext("2d") as CanvasRenderingContext2D;
  const c = S / 2;
  const glow = ctx.createRadialGradient(c, c, 2, c, c, c);
  glow.addColorStop(0, hexA(color, sel ? 0.5 : 0.3));
  glow.addColorStop(1, hexA(color, 0));
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(c, c, c, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = sel ? "#fff6d6" : color;
  ctx.lineWidth = sel ? 7 : 5;
  ctx.globalAlpha = 0.92;
  ctx.beginPath();
  ctx.arc(c, c, c * 0.46, 0, Math.PI * 2);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.fillStyle = sel ? "#fff6d6" : color;
  ctx.beginPath();
  ctx.arc(c, c, c * 0.15, 0, Math.PI * 2);
  ctx.fill();
  const tex = new THREE.CanvasTexture(cv);
  tex.anisotropy = 4;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  return new THREE.Sprite(mat);
}

const props = defineProps<{ nodes: GraphNode[]; edges: GraphEdge[]; selected?: string }>();
const emit = defineEmits<{ select: [ref: string] }>();

const container = ref<HTMLElement | null>(null);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let graph: any = null;
let ro: ResizeObserver | null = null;

// node reticle scale per type (kept small — the user found fat spheres distracting)
const NODE_VAL: Record<string, number> = {
  faction: 5.5,
  region: 4,
  location: 3.2,
  poi: 3.2,
  npc: 3.2,
  event: 3,
  term: 2.8,
  concept: 2.8,
};

function toData(): { nodes: unknown[]; links: unknown[] } {
  const nodes = props.nodes.map((n) => ({
    id: n.ref,
    ref: n.ref,
    label: n.label,
    kind: n.kind,
    val: NODE_VAL[n.kind] ?? 3,
    flag: n.flag,
  }));
  const ids = new Set(nodes.map((n) => n.id));
  const links = props.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
    .map((e) => ({
      source: e.source,
      target: e.target,
      kind: e.kind,
      cat: edgeCat(e.kind ?? ""),
      label: e.label ?? e.kind,
    }));
  return { nodes, links };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function nodeFill(n: any): string {
  if (n.ref === props.selected) return "#fff6d6";
  if (n.flag === "must_change") return "#e0705a";
  if (n.flag === "suggest_check") return "#e0a878";
  return kindColor(n.kind);
}

function build(): void {
  if (!container.value) return;
  graph = new ForceGraph3D(container.value)
    .backgroundColor("rgba(0,0,0,0)")
    .showNavInfo(false)
    // replace the default sphere entirely with a minimalist reticle + label
    .nodeThreeObjectExtend(false)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .nodeThreeObject((n: any) => {
      const group = new THREE.Group();
      const sel = n.ref === props.selected;
      const reticle = makeReticle(nodeFill(n), sel);
      const s = (n.val ?? 3) * 2.4;
      reticle.scale.set(s, s, 1);
      group.add(reticle);
      const label = new SpriteText(n.label);
      label.color = sel ? "#fff6d6" : "#cfd6e6";
      label.textHeight = 3.8;
      label.fontFace = "Noto Sans SC, sans-serif";
      label.fontWeight = "600";
      label.strokeWidth = 0.5;
      label.strokeColor = "rgba(8,11,28,0.92)";
      label.position.y = -(s * 0.62 + 3);
      label.material.depthWrite = false;
      group.add(label);
      return group;
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .linkColor((l: any) => EDGE_CAT_META[l.cat as keyof typeof EDGE_CAT_META].color)
    .linkOpacity(0.55)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .linkWidth((l: any) => (l.cat === "affiliation" ? 1.1 : 0.6))
    .linkCurvature(0.12)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .linkDirectionalArrowLength((l: any) => (EDGE_CAT_META[l.cat as keyof typeof EDGE_CAT_META].arrow ? 3.4 : 0))
    .linkDirectionalArrowRelPos(0.86)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .linkDirectionalParticles((l: any) => (EDGE_CAT_META[l.cat as keyof typeof EDGE_CAT_META].flow ? 2 : 0))
    .linkDirectionalParticleWidth(1.6)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .linkDirectionalParticleColor((l: any) => EDGE_CAT_META[l.cat as keyof typeof EDGE_CAT_META].color)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .onNodeClick((n: any) => emit("select", n.ref))
    .graphData(toData());
  // a touch slower so it settles gently into depth
  graph.d3VelocityDecay(0.28);
  resize();
}

function resize(): void {
  if (!graph || !container.value) return;
  graph.width(container.value.clientWidth).height(container.value.clientHeight);
}

function refreshStyles(): void {
  if (!graph) return;
  // re-applying the accessor rebuilds each node's reticle + label with the new selected state
  graph.nodeThreeObject(graph.nodeThreeObject());
}

onMounted(async () => {
  await nextTick();
  build();
  ro = new ResizeObserver(resize);
  if (container.value) ro.observe(container.value);
});

onBeforeUnmount(() => {
  ro?.disconnect();
  if (graph) {
    graph._destructor?.();
    graph = null;
  }
});

watch(
  () => [props.nodes, props.edges],
  () => {
    if (graph) graph.graphData(toData());
  },
);
watch(
  () => props.selected,
  () => refreshStyles(),
);
</script>

<template>
  <div ref="container" class="graph3d"></div>
</template>

<style scoped>
.graph3d {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
/* absolutely positioned so the canvas's intrinsic pixel size never drives (and collapses) the
   container/section layout — the container sizes from its parent, the canvas fills it. */
.graph3d :deep(canvas) {
  position: absolute;
  inset: 0;
  display: block;
  outline: none;
}
</style>
