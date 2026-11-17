<script setup lang="ts">
// Background astrolabe: a slow-rotating tick-ring star disk (SVG, CSS-driven) plus a canvas of stars
// that orbit the disk centre, each leaving a fading trail (HSR's "star rail" feel). Sits behind all
// content at low opacity so it adds depth without stealing focus. Fully off under reduced-motion.
import { onMounted, onUnmounted, ref } from "vue";

const canvas = ref<HTMLCanvasElement | null>(null);
const reduced =
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

interface Star {
  angle: number;
  radius: number;
  speed: number;
  size: number;
  tint: string;
}

let raf = 0;
let stars: Star[] = [];
let w = 0;
let h = 0;

function resize(c: HTMLCanvasElement): void {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  w = window.innerWidth;
  h = window.innerHeight;
  c.width = Math.floor(w * dpr);
  c.height = Math.floor(h * dpr);
  c.style.width = `${w}px`;
  c.style.height = `${h}px`;
  const ctx = c.getContext("2d");
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

const TINTS = ["#f0d28a", "#b9a7ff", "#8fd6e8", "#ece5d3"];

function seed(): void {
  // orbit centre sits upper-right, echoing the Herta space-station composition
  const count = Math.min(90, Math.round((w * h) / 24000));
  stars = Array.from({ length: count }, () => {
    const radius = 80 + Math.random() * Math.max(w, h) * 0.62;
    return {
      angle: Math.random() * Math.PI * 2,
      radius,
      // outer stars sweep slightly slower — a faint differential rotation
      speed: (0.00018 + Math.random() * 0.00035) * (1 - radius / (Math.max(w, h) * 1.4)),
      size: 0.6 + Math.random() * 1.4,
      tint: TINTS[Math.floor(Math.random() * TINTS.length)],
    };
  });
}

function frame(c: HTMLCanvasElement): void {
  const ctx = c.getContext("2d");
  if (!ctx) return;
  const cx = w * 0.82;
  const cy = h * 0.16;
  // fade prior frame toward transparent → trails, while keeping the page background showing through
  ctx.globalCompositeOperation = "destination-out";
  ctx.fillStyle = "rgba(0,0,0,0.055)";
  ctx.fillRect(0, 0, w, h);
  // draw stars additively so overlaps read as light, not paint
  ctx.globalCompositeOperation = "lighter";
  for (const s of stars) {
    s.angle += s.speed;
    const x = cx + Math.cos(s.angle) * s.radius;
    const y = cy + Math.sin(s.angle) * s.radius * 0.92;
    if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
    ctx.beginPath();
    ctx.fillStyle = s.tint;
    ctx.globalAlpha = 0.5;
    ctx.arc(x, y, s.size, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  raf = window.requestAnimationFrame(() => frame(c));
}

function onResize(): void {
  const c = canvas.value;
  if (!c) return;
  resize(c);
  seed();
}

onMounted(() => {
  const c = canvas.value;
  if (!c || reduced) return;
  resize(c);
  seed();
  window.addEventListener("resize", onResize, { passive: true });
  raf = window.requestAnimationFrame(() => frame(c));
});

onUnmounted(() => {
  window.cancelAnimationFrame(raf);
  window.removeEventListener("resize", onResize);
});
</script>

<template>
  <div class="starfield" aria-hidden="true">
    <svg class="astrolabe" viewBox="0 0 200 200" preserveAspectRatio="xMidYMid slice">
      <g class="disk">
        <circle cx="100" cy="100" r="94" />
        <circle cx="100" cy="100" r="72" stroke-dasharray="1 4" />
        <circle cx="100" cy="100" r="50" stroke-dasharray="0.6 6" />
        <g class="ticks">
          <line v-for="n in 24" :key="n" x1="100" y1="6" x2="100" y2="13"
            :transform="`rotate(${(360 / 24) * n} 100 100)`" />
        </g>
      </g>
    </svg>
    <canvas ref="canvas" class="stars"></canvas>
  </div>
</template>

<style scoped>
.starfield {
  position: fixed;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  overflow: hidden;
}
.stars {
  position: absolute;
  inset: 0;
  opacity: 0.75;
}
/* the rotating tick-ring disk, anchored to the same upper-right centre the stars orbit */
.astrolabe {
  position: absolute;
  right: -34vmax;
  top: -44vmax;
  width: 96vmax;
  height: 96vmax;
  opacity: 0.16;
  color: var(--ow-gold);
}
.astrolabe .disk {
  transform-origin: 100px 100px;
  animation: astro-spin 240s linear infinite;
}
.astrolabe circle {
  fill: none;
  stroke: currentColor;
  stroke-width: 0.3;
}
.astrolabe .ticks line {
  stroke: var(--ow-violet);
  stroke-width: 0.4;
}
@keyframes astro-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .astrolabe .disk {
    animation: none;
  }
  .stars {
    display: none;
  }
}
</style>
