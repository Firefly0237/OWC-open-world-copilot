<script setup lang="ts">
defineProps<{ size?: number }>();
</script>

<template>
  <svg :width="size ?? 56" :height="size ?? 56" viewBox="0 0 100 100" fill="none" class="emblem">
    <circle class="orb" cx="50" cy="50" r="44" stroke="#8fd6e8" stroke-opacity=".35" stroke-dasharray="3 6" />
    <circle class="orb-rev" cx="50" cy="50" r="38" stroke="#b9a7ff" stroke-opacity=".3" stroke-dasharray="1 7" />
    <circle cx="50" cy="50" r="30" stroke="#d9b56c" stroke-opacity=".4" />
    <!-- orbiting spark dots: the HSR "secondary motion" finish -->
    <g class="sparks">
      <circle cx="50" cy="8" r="2" fill="#f0d28a" />
      <circle cx="92" cy="50" r="1.5" fill="#8fd6e8" />
    </g>
    <path
      class="core"
      d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50 L43.5 43.5 Z"
      fill="#d9b56c"
      fill-opacity=".25"
      stroke="#f0d28a"
      stroke-opacity=".85"
    />
  </svg>
</template>

<style scoped>
.orb,
.orb-rev,
.sparks,
.core {
  transform-origin: center;
  transform-box: fill-box;
}
.orb {
  animation: emblem-spin 10s linear infinite;
}
.orb-rev {
  animation: emblem-spin 7s linear infinite reverse;
}
.sparks {
  transform-origin: 50px 50px;
  transform-box: view-box;
  animation: emblem-spin 4.5s linear infinite;
}
.core {
  animation: emblem-pulse 2s ease-in-out infinite;
  filter: drop-shadow(0 0 5px rgba(240, 210, 138, 0.5));
}
@keyframes emblem-spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes emblem-pulse {
  0%,
  100% {
    opacity: 0.72;
    transform: scale(0.95);
  }
  50% {
    opacity: 1;
    transform: scale(1.06);
  }
}
@media (prefers-reduced-motion: reduce) {
  .orb,
  .orb-rev,
  .sparks,
  .core {
    animation: none;
  }
}
</style>
